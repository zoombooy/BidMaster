"""评分/要求条目的适用范围模型（项目通用 / 分标通用 / 指定包 / 待确认）。

业务规则：目标包的交付物应包含 项目通用 + 所属分标通用 + 目标包专属 三层内容；
只有**明确**属于其他包的条目才排除，无法判定的保留并标"待确认"。

包号提取只认明确的包号语法（"包01"、"第2包"、"3号包"、"标段4"、区间"包1-包3"），
不把电压等级（500千伏）、年份（2026年度）、普通序号当包号——
提取不到包号的文字一律 unknown 保留，绝不作为排除依据。
"""
from __future__ import annotations

import re

_UNIVERSAL = "universal"   # 项目/分标通用（目标包继承）
_TARGET = "target"         # 明确属于目标包
_OTHER = "other"           # 明确属于其他包（唯一可排除）
_UNKNOWN = "unknown"       # 归属不明（保留 + 待确认标注）

# 明确包号语法（防止"500千伏"/"2026年度"这类数字被误当包号）
_PKG_PATTERNS = [
    re.compile(r"包\s*0*(\d+)\s*号?"),
    re.compile(r"第\s*0*(\d+)\s*包"),
    re.compile(r"(\d+)\s*号包"),
    re.compile(r"标段\s*0*(\d+)"),
]
# 包号区间："包1-包3" / "包1~包3" / "包1至包3"（可出现多个，全部收集）
_RANGE_RE = re.compile(r"包\s*0*(\d+)\s*[-~～至到]\s*(?:包\s*)?0*(\d+)")


def _full2half(s: str) -> str:
    table = str.maketrans("０１２３４５６７８９", "0123456789")
    return (s or "").translate(table)


def package_numbers(lot: str | None) -> set[int]:
    """从明确的包号语法提取包号集合：全部区间 + 全部独立包号取并集；
    提取不到包号语法返回空集（空集不作为排除依据）。"""
    s = _full2half(lot)
    if not s:
        return set()
    nums: set[int] = set()
    rest: list[str] = []
    last = 0
    for m in _RANGE_RE.finditer(s):
        rest.append(s[last:m.start()])
        lo, hi = int(m.group(1)), int(m.group(2))
        if hi >= lo:
            nums.update(range(lo, hi + 1))
        last = m.end()
    rest.append(s[last:])
    remainder = " ".join(rest)
    for pat in _PKG_PATTERNS:
        for m in pat.finditer(remainder):
            nums.add(int(m.group(1)))
    return nums


def lot_number(lot: str | None) -> int | None:
    ns = package_numbers(lot)
    return min(ns) if ns else None


def read_lot(item, attr: str = "parsed_rule") -> str:
    """评分项包归属统一读取：ScoreItem 无顶层 lot，写在 parsed_rule["lot"]。"""
    if item is None:
        return ""
    v = item.get(attr) if isinstance(item, dict) else getattr(item, attr, None)
    if isinstance(v, dict):
        return str(v.get("lot") or "")
    return str(v or "")


def split_fenbiao(lot: str | None) -> tuple[str, str]:
    """复合键 "分标::包号" 拆分；非复合键返回 ("", 原值)。"""
    s = lot or ""
    if "::" in s:
        fb, bare = s.split("::", 1)
        return fb.strip(), bare.strip()
    return "", s.strip()


def same_lot(a: str, b: str) -> bool:
    """同包判定：字符串相等；或分标一致（双方都无分标视为一致）且包号集合有交集。
    分标明确冲突 → False。缺分标依据的场景请走 classify（返回待确认）。"""
    if not a or not b:
        return False
    if a == b:
        return True
    fa, ba = split_fenbiao(a)
    fb_, bb = split_fenbiao(b)
    if (fa or fb_) and fa != fb_:
        return False
    na, nb = package_numbers(ba), package_numbers(bb)
    return bool(na and nb and na & nb)


def classify(lot_str: str, target_lot: str) -> tuple[str, str]:
    """返回 (scope, label)。仅 other 可排除；unknown 保留并标待确认。
    分标三元判定：双方分标明确且不同 → other；仅一方有分标 → 依据不足
    → unknown 待确认（不凭包号数字直接判目标包）。"""
    lot_str = (lot_str or "").strip()
    if not lot_str or lot_str == "-":
        return _UNKNOWN, "待确认"
    fa, ba = split_fenbiao(lot_str)
    fbt, bt = split_fenbiao(target_lot)
    if same_lot(lot_str, target_lot):
        t_nums = package_numbers(bt)
        l_nums = package_numbers(ba)
        if t_nums and l_nums and len(l_nums) > 1:  # 区间/列表覆盖多包
            return _UNIVERSAL, f"通用·含目标包（{lot_str[:14]}）"
        return _TARGET, "目标包"
    if fa and fbt and fa != fbt:
        # 双方分标明确不同：即使包号数字相同也不是同一个包
        return _OTHER, f"其他分标·{lot_str[:12]}"
    t_nums = package_numbers(bt)
    l_nums = package_numbers(ba)
    if t_nums and l_nums and not (t_nums & l_nums):
        # 双方分标一致（或均无）且包号无交集 → 明确其他包
        return _OTHER, f"其他包·{ba[:14]}"
    # 分标依据不足 / 无包号语法 → 待确认保留
    return _UNKNOWN, f"待确认·{lot_str[:16]}"


def filter_for_target(items: list, target_lot: str):
    """统一筛选入口。返回 (有效条目, 排除条目)；
    unknown/universal/target 保留，仅 other 排除。"""
    keep, dropped = [], []
    for it in items:
        scope, _ = classify(read_lot(it), target_lot)
        (dropped if scope == _OTHER else keep).append(it)
    return keep, dropped


def scope_pending(items: list, target_lot: str) -> list:
    """返回 unknown（待确认）条目列表——交付物需对这些条目标注不确定性。"""
    out = []
    for it in items:
        scope, _ = classify(read_lot(it), target_lot)
        if scope == _UNKNOWN:
            out.append(it)
    return out
