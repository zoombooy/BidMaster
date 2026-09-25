"""归一化：中文大写金额/日期/工期 → 结构化值。

- 金额：`人民币贰亿伍仟万元整（¥250,000,000.00）` / `3,500.5万元` / `1.2亿` → 元(float)
- 日期：`2026年11月20日09时30分` / `二〇二六年十月一日` / `2026-11-20` → ISO
- 工期：`540日历天` / `三个月` → 天数
"""
from __future__ import annotations

import re

# ---------- 中文数字 ----------
_CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
              "壹": 1, "贰": 2, "叁": 3, "肆": 4, "伍": 5, "陆": 6, "柒": 7,
              "捌": 8, "玖": 9}
_CN_UNITS = {"十": 10, "拾": 10, "百": 100, "佰": 100, "千": 1000, "仟": 1000}
_CN_BIG = {"万": 10000, "亿": 10 ** 8, "兆": 10 ** 12}


def cn_to_number(text: str) -> float | None:
    """中文数字（位值制，用于金额）→ 数值。例：伍佰→500，三千五百→3500，二十万→200000。"""
    total, section, num = 0, 0, 0
    for ch in text:
        if ch in _CN_DIGITS:
            num = _CN_DIGITS[ch]
        elif ch in _CN_UNITS:
            if num == 0:
                num = 1  # 十 = 10
            section += num * _CN_UNITS[ch]
            num = 0
        elif ch in _CN_BIG:
            section = (section + num) * _CN_BIG[ch]
            total += section
            section, num = 0, 0
        elif ch in ("点", "．", "."):
            break  # 中文大写金额不处理小数（用角分），遇点停止
        # 整/正/圆/元 等忽略
    return float(total + section + num) if (total + section + num) else None


def _cn_digit_concat(text: str) -> int | None:
    """中文数字（连写制，用于日期年份如 二〇二六）→ 整数。"""
    vals = [_CN_DIGITS.get(ch) for ch in text]
    if not vals or any(v is None for v in vals):
        return None
    out = 0
    for v in vals:  # type: ignore
        out = out * 10 + v
    return out


def _part_to_int(part: str) -> int | None:
    """日期分量转整数：'二〇二六'→2026，'二十'→20，'5'→5，'三十'→30。"""
    part = part.strip()
    if not part:
        return None
    if part.isdigit():
        return int(part)
    if all(ch in _CN_DIGITS for ch in part) and not any(ch in _CN_UNITS for ch in part):
        return _cn_digit_concat(part)
    return int(cn_to_number(part) or 0) or None


def normalize_amount(text: str) -> tuple[float, str] | None:
    """金额 → (数值/元, 原始文本)。支持大写、万元/亿、千分位、括号内小写。"""
    if not text:
        return None
    raw = text.strip()

    # 优先取大写部分（更可靠）：大写：伍佰万元整 / 直接的中文大写数字串
    m = re.search(r"[大写][：:]?\s*([^（）()，,;；\s]+)", raw)
    cn_part = None
    if m and any(ch in _CN_DIGITS or ch in _CN_UNITS or ch in _CN_BIG for ch in m.group(1)):
        cn_part = m.group(1)
    else:
        m2 = re.search(r"[零〇一二两三四五六七八九壹贰叁肆伍陆柒捌玖拾佰仟万亿]{2,}", raw)
        if m2:
            cn_part = m2.group(0)

    if cn_part:
        unit = 1
        rest = cn_part
        # 中文数字串内的 万/亿 已在 cn_to_number 处理；若后面跟 "万元" 已包含
        val = cn_to_number(rest.replace("元", "").replace("整", "").replace("圆", ""))
        if val is not None and val > 0:
            return round(val, 2), raw

    # 阿拉伯数字路径：1,234,567.89 / 3500.5万元 / 1.2亿
    t = raw.replace("人民币", "").replace("¥", "").replace("￥", "").replace(" ", "")
    unit = 1
    if "亿元" in t or t.endswith("亿"):
        unit = 10 ** 8
    elif "万元" in t or t.endswith("万"):
        unit = 10000
    m = re.search(r"(\d[\d,，]*(?:\.\d+)?)", t)
    if not m:
        return None
    val = float(m.group(1).replace(",", "").replace("，", "")) * unit
    if val > 0:
        return round(val, 2), raw
    return None


_DATE_RE = re.compile(
    r"((?:\d{4}|[零〇一二三四五六七八九]{4})\s*年)?\s*"
    r"((?:\d{1,2}|[一二三四五六七八九十]{1,3})\s*月)?\s*"
    r"((?:\d{1,2}|[一二三四五六七八九十]{1,4})\s*日号?)?")
_TIME_RE = re.compile(r"(\d{1,2})[时点:：](\d{1,2})分?(?::(\d{1,2}))?")
_DATE_ISO_RE = re.compile(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})")


def normalize_datetime(text: str) -> tuple[str, str] | None:
    """日期时间 → (ISO 字符串, 原始文本)。'2026年11月20日09时30分'→'2026-11-20 09:30'。"""
    if not text:
        return None
    raw = text.strip()

    m = _DATE_ISO_RE.search(raw)
    y, mo, d = 0, 0, 0
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    else:
        m = _DATE_RE.search(raw)
        if not m or not (m.group(1) or m.group(2)):
            return None
        if m.group(1):
            y = _part_to_int(re.sub(r"\s|年", "", m.group(1))) or 0
        if m.group(2):
            mo = _part_to_int(re.sub(r"\s|月", "", m.group(2))) or 0
        if m.group(3):
            d = _part_to_int(re.sub(r"\s|[日号]", "", m.group(3))) or 0
    if not y or not (1 <= mo <= 12) or not (1 <= d <= 31):
        return None
    out = f"{y:04d}-{mo:02d}-{d:02d}"
    tm = _TIME_RE.search(raw)
    if tm:
        out += f" {int(tm.group(1)):02d}:{int(tm.group(2)):02d}"
        if tm.group(3):
            out += f":{int(tm.group(3)):02d}"
    return out, raw


_DUR_DAYS_RE = re.compile(r"(\d+)\s*(?:个)?(日历天|历天|工作日|天|日)")
_DUR_MONTH_CN = re.compile(r"([一二三四五六七八九十]{1,3}|\d{1,2})\s*个?月")
_DUR_YEAR_CN = re.compile(r"([一二三四五六七八九十]{1,3}|\d{1,2})\s*年(?![一二三四五六七八九〇]{3}\s*月)")


def normalize_duration(text: str) -> tuple[int, str, str] | None:
    """工期 → (天数, 单位文本, 原始文本)。月按 30 天、年按 365 天折算（保留原始单位）。"""
    if not text:
        return None
    raw = text.strip()
    m = _DUR_DAYS_RE.search(raw)
    if m:
        return int(m.group(1)), m.group(2), raw
    m = _DUR_MONTH_CN.search(raw)
    if m:
        n = _part_to_int(m.group(1)) or 0
        if n:
            return n * 30, "月", raw
    m = _DUR_YEAR_CN.search(raw)
    if m:
        n = _part_to_int(m.group(1)) or 0
        if n:
            return n * 365, "年", raw
    return None
