"""字段级精细校验器（对标 tender-extract 的类型化清洗器/校验器设计）。

设计原则：
- 硬校验（不合法直接淘汰）与软校验（合法但降置信度）分离；
- 所有"比较"先经 normalize_for_compare 归一化；
- 无法可靠判断的返回 None/False，不猜测。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

# ---------- 通用归一化 ----------
_FULLWIDTH = str.maketrans("，：；（）［］　", ",:;()[] ")


def normalize_for_compare(v: str) -> str:
    """strip + casefold + 全角标点转半角 + 去所有空白 + 去零宽字符。"""
    v = (v or "").translate(_FULLWIDTH).casefold().strip()
    return re.sub(r"[\s\u200b\u200c\u200d\ufeff]+", "", v)


# ---------- 占位词 / 无意义值 ----------
PLACEHOLDERS = {"无", "暂无", "略", "详见", "见附件", "见招标公告", "见投标人须知前附表",
                "见前附表", "详见附件", "详见下表", "详见第五章", "/", "—", "-", "无要求"}


def is_placeholder(v: str) -> bool:
    return normalize_for_compare(v) in {normalize_for_compare(p) for p in PLACEHOLDERS}


def repetition_ratio(v: str) -> float:
    """字符多样性占比；< 0.3 视为无意义值（如"——————"）。"""
    v = re.sub(r"\s", "", v)
    if not v:
        return 0.0
    return len(set(v)) / len(v)


def is_meaningful(v: str) -> bool:
    return bool(v) and not is_placeholder(v) and repetition_ratio(v) >= 0.3


# ---------- 身份证（GB11643 校验位） ----------
_ID_WEIGHTS = [7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2]
_ID_CHECK = "10X98765432"
_ID_RE = re.compile(r"^\d{17}[\dXx]$")


def is_valid_id_card(id_no: str) -> bool:
    """18 位身份证：格式 + 出生日期段 + GB11643 校验位。"""
    id_no = (id_no or "").strip().upper()
    if not _ID_RE.match(id_no):
        return False
    if not re.match(r"^(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])", id_no[6:14]):
        return False
    checksum = sum(int(ch) * w for ch, w in zip(id_no[:17], _ID_WEIGHTS))
    return _ID_CHECK[checksum % 11] == id_no[17]


# ---------- 组织/公司后缀 ----------
_COMPANY_SUFFIX = re.compile(
    r"(?:有限公司|有限责任公司|股份公司|集团公司|集团|公司|企业|"
    r"人民政府|委员会|管理局|财政局|教育局|住建局|中心|医院|学院|大学|研究院|研究所|事务所)$")


def looks_like_organization(v: str) -> bool:
    """公司/机构后缀白名单（strip 后以后缀结尾）。"""
    v = (v or "").strip()
    return bool(v) and bool(_COMPANY_SUFFIX.search(v))


# ---------- 金额 / 日期合理区间 ----------
AMOUNT_RANGE = {"amount": (1.0, 1e10),          # 1 元 ~ 100 亿
                "deposit": (1000.0, 1e9)}        # 1 千 ~ 10 亿


def amount_in_range(v: float, lo: float, hi: float) -> bool:
    return v is not None and lo <= v <= hi


def date_in_window(v: str, past_days: int = 3650, future_days: int = 1825) -> bool:
    """ISO 日期(可带时间)落在 [now-past_days, now+future_days] 内。"""
    try:
        dt = datetime.strptime((v or "")[:10], "%Y-%m-%d")
    except ValueError:
        return False
    now = datetime.now()
    return now - timedelta(days=past_days) <= dt <= now + timedelta(days=future_days)


# ---------- 具体格式 ----------
def is_valid_credit_code(v: str) -> bool:
    """统一社会信用代码：18 位，字符集 0-9 A-H J-N P-R T-U W-Y（不含 I O S V Z）。"""
    return bool(re.fullmatch(r"[0-9A-HJ-NPQRTUWXY]{18}", (v or "").strip().upper()))


def is_valid_phone(v: str) -> bool:
    v = (v or "").strip()
    return bool(re.fullmatch(r"(?:1[3-9]\d{9}|0\d{2,3}-?\d{7,8}(?:-\d{1,5})?)", v))


def is_person_name(v: str) -> bool:
    v = (v or "").strip()
    return bool(re.fullmatch(r"[\u4e00-\u9fa5]{2,4}", v))


def is_tender_no_like(v: str) -> bool:
    """编号：4-40 位字母/数字/常见分隔符，且必含字母或数字。"""
    return bool(re.fullmatch(r"[A-Za-z0-9\-_—－()（）/、.]{4,40}", (v or "").strip())) \
        and bool(re.search(r"[0-9A-Za-z]", v))


# ---------- 冲突检测辅助 ----------
def numeric_spread_note(values: list[float]) -> str | None:
    """数值候选冲突描述：最大/最小比 > 5 → 疑似多分标；唯一值 ≥ 4 → 多处条款。"""
    vals = [v for v in values if isinstance(v, (int, float)) and v > 0]
    if len(vals) >= 2:
        hi, lo = max(vals), min(vals)
        if lo > 0 and hi / lo > 5:
            return f"数值差异过大（最高 {hi:g} / 最低 {lo:g}，比值 >5），可能为不同分标/包件的数据"
    if len(set(vals)) >= 4:
        return f"存在 {len(set(vals))} 个不同取值，可能来自不同分标或多处条款"
    return None
