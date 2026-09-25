"""中文文档编号体系识别与归一化：第X章 / 1. / 1.1 / （一） / 一、 / 1．。"""
from __future__ import annotations

import re

CN_CHARS = "一二三四五六七八九十百"
CN_NUM = rf"[{CN_CHARS}]+"

# 每个模式返回 (level, 归一化编号)；按优先级排列
_PATTERNS: list[tuple[re.Pattern, int, int]] = [
    # (regex, level_base, group_of_number)
    (re.compile(rf"^第\s*({CN_NUM}|\d+)\s*[章编]"), 1, 1),
    (re.compile(rf"^第\s*({CN_NUM}|\d+)\s*[节条]"), 2, 1),
    (re.compile(rf"^（[{CN_CHARS}]+）"), 3, 0),  # （一）/（十一）无编号捕获，仅定级
    (re.compile(rf"^[一二三四五六七八九十]+、"), 2, 0),
    (re.compile(r"^(\d{1,2})\.(\d{1,2})\.(\d{1,2})[^\d.]"), 4, 0),
    (re.compile(r"^(\d{1,2})\.(\d{1,2})[^\d.]"), 3, 0),
    (re.compile(r"^(\d{1,2})[、．.]\s*\S"), 2, 1),
]

_CN_DIGIT = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7,
             "八": 8, "九": 9, "十": 10}


def cn_ordinal_to_int(text: str) -> int:
    """简单中文序数（一~九十九）转整数，失败返回 0。"""
    text = text.strip()
    if text.isdigit():
        return int(text)
    if text in _CN_DIGIT:
        return _CN_DIGIT[text]
    if "十" in text:
        left, _, right = text.partition("十")
        l = _CN_DIGIT.get(left, 1) if left else 1
        r = _CN_DIGIT.get(right, 0) if right else 0
        return l * 10 + r
    return 0


def detect_numbering(text: str) -> tuple[int, str] | None:
    """检测行首编号 → (level, 归一化编号如 '3.2')；非编号返回 None。"""
    for idx, (pat, level, num_group) in enumerate(_PATTERNS):
        m = pat.match(text)
        if not m:
            continue
        if idx == 4:  # N.N.N
            return 4, f"{m.group(1)}.{m.group(2)}.{m.group(3)}"
        if idx == 5:  # N.N
            return 3, f"{m.group(1)}.{m.group(2)}"
        if num_group:
            n = cn_ordinal_to_int(m.group(num_group))
            return level, str(n) if n else m.group(num_group)
        return level, ""
    return None


def strip_number(text: str) -> str:
    """去掉行首编号，返回纯标题文本。"""
    for pat, _, _ in _PATTERNS:
        m = pat.match(text)
        if m:
            return text[m.end():].strip("　 .。、：:")
    return text
