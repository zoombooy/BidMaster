"""评分标准的 LLM 兜底通道：解析散文式/公式式评分细则（如前附表之五的价格公式 blob）。

与字段兜底同一套纪律：模型必须给 source_quote，代码回查原文——引用找不到 = 丢弃，
无证据不采纳。规则层已确信的（confirmed）不再调用，只在 pending_review 上花 token。
"""
from __future__ import annotations

from bidmaster.llm.client import LLMClient
from bidmaster.schemas.document import ParsedDocument
from bidmaster.schemas.evidence import EvidenceStore, make_evidence
from bidmaster.schemas.scoring import ScoreItem

_SYSTEM = (
    "你是专业的评标办法分析助手。把给定的价格评分标准散文解析为结构化公式列表。"
    "只依据原文，禁止编造。每个公式输出 {\"name\": 公式名, \"expression\": 价格得分计算式, "
    "\"baseline\": 基准价/基准值定义}。同时输出 \"quote\"：原文中支撑这些公式的原句（必须逐字来自原文）。"
    "只输出 JSON：{\"formulas\": [...], \"quote\": \"...\"}"
)


def refine_pending_scores(parsed: ParsedDocument, scores: list[ScoreItem],
                          store: EvidenceStore, llm: LLMClient) -> int:
    """对 pending_review 的评分项做 LLM 结构化，返回升级数量。"""
    pending = [s for s in scores if s.status == "pending_review"]
    if not pending or not llm.enabled:
        return 0
    upgraded = 0
    full_text = parsed.full_text
    for s in pending:
        user = (
            f"价格评分标准原文：\n{s.rule_text[:2000]}\n\n"
            "请解析为结构化公式列表（含 quote 原文引用）。"
        )
        data = llm.chat_json(_SYSTEM, user)
        if not isinstance(data, dict):
            continue
        quote = str(data.get("quote", "")).strip()
        formulas = data.get("formulas")
        if not quote or not isinstance(formulas, list) or not formulas:
            continue
        # 证据回查：quote 前缀必须能在原文找到
        pos = full_text.find(quote[:15])
        if pos < 0:
            s.note = "LLM 解析结果的引用无法回查原文，维持 pending_review"
            continue
        ev = store.add(make_evidence(
            kind="llm_quote", source="llm", char_start=pos,
            char_end=pos + len(quote), snippet=quote[:200]))
        s.parsed_rule = {**s.parsed_rule,
                         "formulas": [f for f in formulas if isinstance(f, dict)]}
        s.formula = "；".join(
            str(f.get("name", "")) + "：" + str(f.get("expression", ""))
            for f in formulas if isinstance(f, dict))[:500]
        s.evidence_ids = list(dict.fromkeys(s.evidence_ids + [ev.evidence_id]))
        s.confidence = 0.72
        s.status = "confirmed"
        upgraded += 1
    return upgraded
