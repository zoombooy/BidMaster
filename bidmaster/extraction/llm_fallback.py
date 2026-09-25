"""LLM 兜底抽取：仅对规则未命中的字段发起；要求模型给出 source_quote，
代码在原文中回查（前缀匹配）验证——引用查不到 = 丢弃该值（无证据不采纳）。"""
from __future__ import annotations

from bidmaster.extraction.normalize import normalize_amount, normalize_datetime, normalize_duration
from bidmaster.extraction.rules import NORM_AMOUNT, NORM_DATETIME, NORM_DURATION, FIELD_RULES
from bidmaster.llm.client import LLMClient
from bidmaster.schemas.document import ParsedDocument
from bidmaster.schemas.evidence import EvidenceStore, make_evidence
from bidmaster.schemas.fields import STATUS_FAILED, STATUS_FOUND, FieldCandidate, FieldExtraction
from bidmaster.schemas.report import AnchorZone

_SYSTEM = (
    "你是专业的投标资料分析助手。只依据用户提供的上下文回答，禁止编造。"
    "对每个字段输出 {\"value\": 原文字符串, \"quote\": 支撑该值的原文原句}；"
    "上下文中确实没有的字段直接省略，不要猜测。只输出 JSON。"
)

_CTX_CHARS = 6000  # 每个锚区注入上下文上限


def _zone_text(parsed: ParsedDocument, zones: list[AnchorZone]) -> str:
    parts: list[str] = []
    for z in zones:
        blocks = parsed.blocks[z.block_start:z.block_end]
        t = "\n".join(b.text for b in blocks)
        parts.append(f"【{z.title or z.zone}】\n{t[:_CTX_CHARS]}")
    return "\n\n".join(parts) if parts else parsed.full_text[:_CTX_CHARS]


def llm_fill_missing(parsed: ParsedDocument, zones: list[AnchorZone],
                     missing_keys: list[str], results: dict[str, FieldExtraction],
                     store: EvidenceStore, llm: LLMClient) -> None:
    catalog = {k: FIELD_RULES[k] for k in missing_keys}
    spec = {k: {"normalizer": p.normalizer} for k, p in catalog.items()}
    user = (
        f"请从以下招标文件上下文中抽取这些字段：{list(spec.keys())}\n"
        f"归一化要求说明：amount=金额(元)、datetime=日期时间、duration=工期、text=原文文本。\n"
        f"输出格式：{{\"字段名\": {{\"value\": \"...\", \"quote\": \"...\"}}, ...}}\n\n"
        f"上下文：\n{_zone_text(parsed, zones)}"
    )
    data = llm.chat_json(_SYSTEM, user)
    if not isinstance(data, dict):
        return

    full_text = parsed.full_text
    for key in missing_keys:
        item = data.get(key)
        if not isinstance(item, dict):
            continue
        value = str(item.get("value", "")).strip()
        quote = str(item.get("quote", "")).strip()
        if not value or not quote:
            continue
        # 证据回查：quote 前 15 字符必须能在原文中找到
        pos = full_text.find(quote[:15])
        if pos < 0:
            f = results[key]
            f.status = STATUS_FAILED
            f.note = "LLM 返回值无法回查到原文，已拒绝采纳（无证据不采纳）"
            continue
        pack = catalog[key]
        norm_str = _normalize_for(key, value)
        ev = store.add(make_evidence(
            kind="llm_quote", source="llm", char_start=pos,
            char_end=pos + len(quote), snippet=quote[:200]))
        f = results[key]
        f.status = STATUS_FOUND
        f.value_raw = value
        f.value_normalized = norm_str
        f.value_unit = {NORM_AMOUNT: "元", NORM_DATETIME: "datetime",
                        NORM_DURATION: "天"}.get(pack.normalizer, "text")
        f.confidence = 0.72  # LLM 兜底固定低于规则通道
        f.evidence_ids = [ev.evidence_id]
        f.candidates.append(FieldCandidate(
            value_raw=value, value_normalized=norm_str or "", source="llm",
            confidence=0.72, evidence_id=ev.evidence_id, zone="llm"))


def _normalize_for(field_key: str, value: str) -> str | None:
    pack = FIELD_RULES.get(field_key)
    if pack is None:
        return value
    if pack.normalizer == NORM_AMOUNT:
        r = normalize_amount(value)
        if not r:
            return None
        v = r[0]
        return str(int(v)) if float(v).is_integer() else str(v)
    if pack.normalizer == NORM_DATETIME:
        r = normalize_datetime(value)
        return r[0] if r else None
    if pack.normalizer == NORM_DURATION:
        r = normalize_duration(value)
        return f"{r[0]}{r[1]}" if r else None
    return value
