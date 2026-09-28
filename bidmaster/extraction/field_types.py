"""字段类型注册表：类型化清洗器 + 类型化合并策略（对标 tender-extract
extraction_engine._clean_* + merge.py FieldMerger 的架构）。

每个值形态字段（amount/date/number/contact/person/org/credit_code/text）一条：
  cleaner(value, field_key) -> str | None      候选清洗（不合格返回 None = 淘汰）
  merger(cands, field_key) -> FieldCandidate   类型化合并（逐候选筛合理者）
  placeholders_ok                              占位/引用语是否放行（"见招标公告"
                                               对人/机构字段是待查引用，对代码/金额是垃圾）

文本字段（project_name/quality_standard 等）保持通用路径，不强行类型化。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from bidmaster.extraction import validators as V
from bidmaster.extraction.normalize import normalize_amount, normalize_datetime, normalize_duration
from bidmaster.schemas.fields import FieldCandidate

# ---------- 金额解析（大写/万/亿 → 元） ----------
def _to_yuan(value: str) -> float | None:
    r = normalize_amount(value)
    return r[0] if r else None


def _amount_text(v: str) -> str:
    return str(v).strip()


# ---------- 清洗器 ----------
# 金额值必须以数字/货币符号/大写金额开头（"5.投标保证金…"这类章节序号开头
# 的描述文本不是金额——tender-extract _clean_amount 的语境要求）
_AMOUNT_HEAD = re.compile(r"^(?:[¥￥]|[0-9一二两三四五六七八九壹贰叁肆伍陆柒捌玖拾佰仟万亿零]|人民币)")


def _clean_amount(value: str, field_key: str) -> str | None:
    """金额字段：解析为正数金额 + 开头形态校验 + 内容纯净度
    （去掉数字/单位/大写字符/货币符号后不应残留长文本——"5.投标保证金…"淘汰）。"""
    v = _amount_text(value)
    if not v or not _AMOUNT_HEAD.match(v):
        return None
    residue = re.sub(r"[\d.，,\s元整角分¥￥+-]|[一-鿿]".replace(
        "[一-鿿]", ""), "", v)
    # 简化：只允许出现在金额语境的汉字（大写数字+单位+人民币）
    allowed = set("一二两三四五六七八九壹贰叁肆伍陆柒捌玖拾佰仟万亿零元整角分圆人民币")
    residue = "".join(ch for ch in v if not (ch.isdigit() or ch in allowed
                                             or ch in ".,，。\s()（）¥￥/"))
    if len(residue) > 2:  # 残留 >2 个杂字符（如"投标保证金""扫描件"）→ 不是金额
        return None
    y = _to_yuan(v)
    return v if y is not None and y > 0 else None


def _clean_date(value: str, field_key: str) -> str | None:
    """日期字段：必须能归一化为 ISO。"""
    r = normalize_datetime(_TIME_CLEAN.sub("", value))
    return r[0] if r else None


def _clean_duration(value: str, field_key: str) -> str | None:
    r = normalize_duration(value)
    return f"{r[0]}{r[1]}" if r else None


def _clean_number(value: str, field_key: str) -> str | None:
    """编号字段：字母数字 + 分隔符。"""
    return value.strip() if V.is_tender_no_like(value) else None


def _clean_contact(value: str, field_key: str) -> str | None:
    """联系方式：必须含 ≥3 位连续数字。"""
    return value.strip() if re.search(r"\d{3,}", value) else None


# 表头/回声常用词——对人名字段它们是列头不是值
_PERSON_HEADER_WORDS = {"姓名", "名称", "本人", "签字", "日期", "备注"}


def _clean_person(value: str, field_key: str) -> str | None:
    """人名：2-4 位纯中文，且不是表头回声词（"姓名"是列头不是人名）。"""
    name = re.sub(r"[^\u4e00-\u9fa5·]", "", value)
    if not (2 <= len(name) <= 4):
        return None
    if name in _PERSON_HEADER_WORDS:
        return None
    return name


def _clean_org(value: str, field_key: str) -> str | None:
    """机构：优先后缀白名单；无后缀但长度足够也放行（由合并器降置信）。"""
    v = value.strip()
    if len(v) < 4 or len(v) > 30:
        return None
    return v


def _clean_credit_code(value: str, field_key: str) -> str | None:
    """统一社会信用代码：18 位字符集硬校验——引用语/占位词天然淘汰。"""
    v = value.strip().upper()
    return v if V.is_valid_credit_code(v) else None


def _clean_text(value: str, field_key: str) -> str | None:
    v = value.strip()
    return v if v else None


_TIME_CLEAN = re.compile(r"[（(]下同[）)]|[（(]即[^）)]*[）)]")

# ---------- 合并器 ----------
def _merge_by_parsed_order(cands: list[FieldCandidate], field_key: str,
                           parse, lo: float | None = None, hi: float | None = None) -> FieldCandidate | None:
    """tender-extract _merge_amount_field 模式：按置信度降序，
    取第一个能解析且落在合理区间的候选；全不合格 → None（不硬选）。"""
    for c in sorted(cands, key=lambda x: x.confidence, reverse=True):
        v = parse(str(c.value_normalized or ""))
        if v is None:
            continue
        if lo is not None and not (lo <= v <= hi):
            continue
        return c
    return None


def _merge_amount(cands: list[FieldCandidate], field_key: str) -> FieldCandidate | None:
    lo, hi = V.AMOUNT_RANGE["amount"]
    return _merge_by_parsed_order(cands, field_key,
                                  lambda v: _to_yuan(v), lo, hi)


def _merge_deposit(cands: list[FieldCandidate], field_key: str) -> FieldCandidate | None:
    lo, hi = V.AMOUNT_RANGE["deposit"]
    return _merge_by_parsed_order(cands, field_key,
                                  lambda v: _to_yuan(v), lo, hi)


def _merge_date(cands: list[FieldCandidate], field_key: str) -> FieldCandidate | None:
    for c in sorted(cands, key=lambda x: x.confidence, reverse=True):
        if V.date_in_window(str(c.value_normalized or "")):
            return c
    return None


def _merge_credit(cands: list[FieldCandidate], field_key: str) -> FieldCandidate | None:
    for c in sorted(cands, key=lambda x: x.confidence, reverse=True):
        if V.is_valid_credit_code(str(c.value_normalized or "")):
            return c
    return None


def _merge_default(cands: list[FieldCandidate], field_key: str) -> FieldCandidate | None:
    return max(cands, key=lambda c: c.confidence) if cands else None


# ---------- 占位/引用语策略 ----------
REF_PAT = re.compile(r"^\s*(?:详见?|见)[（(]?[^）)]*(?:公告|前附表|须知|章|附件|规范书|一览表)")


def _ph_ok_always(field_key: str, value: str) -> bool:
    return True


def _ph_reject_for_typed(field_key: str, value: str) -> bool:
    """类型化字段：占位/引用语一律不放行（代码/金额/日期里它们就是垃圾）。"""
    return False


# ---------- 注册表 ----------
# field_key → {cleaner, merger, ph_ok}
_REGISTRY: dict[str, dict] = {
    # 金额类
    **{k: {"cleaner": _clean_amount, "merger": _merge_amount, "ph_ok": _ph_reject_for_typed}
       for k in ("price_limit", "budget_amount", "security_deposit", "performance_bond", "doc_price")},
    # 日期/工期
    "bid_deadline": {"cleaner": _clean_date, "merger": _merge_date, "ph_ok": _ph_reject_for_typed},
    **{k: {"cleaner": _clean_duration, "merger": _merge_default, "ph_ok": _ph_reject_for_typed}
       for k in ("bid_validity", "duration", "warranty_period")},
    # 编号
    "tender_no": {"cleaner": _clean_number, "merger": _merge_default, "ph_ok": _ph_reject_for_typed},
    # 联系方式
    "contact_phone": {"cleaner": _clean_contact, "merger": _merge_default, "ph_ok": _ph_reject_for_typed},
    # 人名
    "legal_representative": {"cleaner": _clean_person, "merger": _merge_default, "ph_ok": _ph_reject_for_typed},
    # 机构
    **{k: {"cleaner": _clean_org, "merger": _merge_default, "ph_ok": _ph_ok_always}
       for k in ("tenderer", "agency")},
    # 信用代码
    "credit_code": {"cleaner": _clean_credit_code, "merger": _merge_credit, "ph_ok": _ph_reject_for_typed},
}


def get_spec(field_key: str) -> dict:
    """字段类型规格；未注册字段走通用清洗 + 默认合并 + 引用语过滤。"""
    return _REGISTRY.get(field_key, {
        "cleaner": _clean_text, "merger": _merge_default, "ph_ok": _ph_ok_always,
    })


def clean_candidate(field_key: str, value: str) -> str | None:
    """候选清洗入口：类型化清洗 + 按字段策略的占位词校验。None = 淘汰。

    org 类字段 ph_ok=always → "见招标公告"作为待查引用放行；
    类型化字段（金额/日期/编号/人名/代码）→ 占位与引用语一律淘汰。
    """
    spec = get_spec(field_key)
    v = spec["cleaner"](value, field_key)
    if v is None:
        return None
    if not spec["ph_ok"](field_key, value):
        if V.is_placeholder(value) or REF_PAT.match(value):
            return None
        if not V.is_meaningful(value):
            return None
    else:
        # 放行字段仍要做基础意义校验（但引用语豁免）
        if V.is_placeholder(value) and not REF_PAT.match(value):
            return None
        if not V.is_meaningful(value) and not REF_PAT.match(value):
            return None
    return v


def merge_candidates(field_key: str, cands: list[FieldCandidate]) -> FieldCandidate | None:
    """类型化合并入口。"""
    return get_spec(field_key)["merger"](cands, field_key)


def is_reference_ok(field_key: str, value: str) -> bool:
    """引用语（'见招标公告'）是否可作为该字段的值。"""
    return get_spec(field_key)["ph_ok"](field_key, value)
