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
# 包号区间："包1-包3" / "包1~包3" / "包1至包3"
_RANGE_RE = re.compile(r"包\s*0*(\d+)\s*[-~～至到]\s*(?:包\s*)?0*(\d+)")


def _full2half(s: str) -> str:
    table = str.maketrans("０１２３４５６７８９", "0123456789")
    return (s or "").translate(table)


def package_numbers(lot: str | None) -> set[int]:
    """从明确的包号语法提取包号集合；区间展开；无包号语法返回空集。"""
    s = _full2half(lot)
    if not s:
        return set()
    m = _RANGE_RE.search(s)
    if m:
        lo, hi = int(m.group(1)), int(m.group(2))
        if hi >= lo:
            return set(range(lo, hi + 1))
    nums: set[int] = set()
    for pat in _PKG_PATTERNS:
        for m in pat.finditer(s):
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


def same_lot(a: str, b: str) -> bool:
    """同包判定：字符串相等，或双方包号集合有交集（包01=包1≠包10）。"""
    if not a or not b:
        return False
    if a == b:
        return True
    na, nb = package_numbers(a), package_numbers(b)
    return bool(na and nb and na & nb)


def classify(lot_str: str, target_lot: str) -> tuple[str, str]:
    """返回 (scope, label)。仅 other 可排除；unknown 保留并标待确认。
    label 为面向用户的展示文本（含"待确认·"/"其他包·"前缀）。"""
    lot_str = (lot_str or "").strip()
    if not lot_str or lot_str == "-":
        return _UNKNOWN, "待确认"
    if same_lot(lot_str, target_lot):
        t_nums = package_numbers(target_lot)
        l_nums = package_numbers(lot_str)
        if t_nums and l_nums and len(l_nums) > 1:  # 区间/列表覆盖多包
            return _UNIVERSAL, f"通用·含目标包（{lot_str[:14]}）"
        return _TARGET, "目标包"
    t_nums = package_numbers(target_lot)
    l_nums = package_numbers(lot_str)
    if t_nums and l_nums:
        if t_nums & l_nums:  # 区间/多包号覆盖目标包 → 通用
            return _UNIVERSAL, f"通用·{lot_str[:16]}"
        return _OTHER, f"其他包·{lot_str[:16]}"
    # 无包号语法 → 分标通用或文字提示：与目标包标识有包含关系视为通用，否则待确认
    if target_lot and (lot_str in target_lot or target_lot in lot_str):
        return _UNIVERSAL, f"通用·{lot_str[:16]}"
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
