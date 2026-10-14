"""评分/要求条目的适用范围模型（项目通用 / 分标通用 / 指定包 / 待确认）。

业务规则：目标包的交付物应包含 项目通用 + 所属分标通用 + 目标包专属 三层内容；
只有**明确**属于其他包（双方都有包号且包号数字不等）的条目才排除。
parsed_rule["lot"] 的实际内容不统一：可能是包号（"包01"）、分标名
（"电网咨询-工程预结算审核"）或表格前短段落提示（"换流站土建施工"），
因此未命中目标包号的字符串一律保守保留并标"待确认"，不做排除。
评分抽取、校验、汇总表导出、目录补充共用本模块，避免各处口径不一。
"""
from __future__ import annotations

import re

_UNIVERSAL = "universal"   # 项目/分标通用（目标包继承）
_TARGET = "target"         # 明确属于目标包
_OTHER = "other"           # 明确属于其他包（唯一可排除）
_UNKNOWN = "unknown"       # 归属不明（保留 + 待确认标注）


def lot_number(lot: str | None) -> int | None:
    m = re.search(r"\d+", (lot or "").replace("０", "0").replace("１", "1")
                  .replace("２", "2").replace("３", "3").replace("４", "4")
                  .replace("５", "5").replace("６", "6").replace("７", "7")
                  .replace("８", "8").replace("９", "9"))
    return int(m.group(0)) if m else None


def read_lot(item, attr: str = "parsed_rule") -> str:
    """评分项包归属统一读取：ScoreItem 无顶层 lot，写在 parsed_rule["lot"]。"""
    if item is None:
        return ""
    v = item.get(attr) if isinstance(item, dict) else getattr(item, attr, None)
    if isinstance(v, dict):
        return str(v.get("lot") or "")
    return str(v or "")


def same_lot(a: str, b: str) -> bool:
    """同包判定：字符串相等，或双方都有包号且数字相等（包01=包1≠包10）。"""
    if not a or not b:
        return False
    if a == b:
        return True
    na, nb = lot_number(a), lot_number(b)
    return na is not None and nb is not None and na == nb


def classify(lot_str: str, target_lot: str) -> tuple[str, str]:
    """返回 (scope, label)。仅 other 可排除；unknown 保留并标待确认。"""
    lot_str = (lot_str or "").strip()
    if not lot_str or lot_str == "-":
        return _UNKNOWN, "待确认"
    if same_lot(lot_str, target_lot):
        return _TARGET, lot_str
    t_num, l_num = lot_number(target_lot), lot_number(lot_str)
    # 双方都有包号且不等 → 明确其他包
    if t_num is not None and l_num is not None and t_num != l_num:
        return _OTHER, lot_str
    # 无包号 → 分标通用或文字提示：与目标包标识有包含关系视为分标通用，否则待确认
    if target_lot and (lot_str in target_lot or target_lot in lot_str):
        return _UNIVERSAL, lot_str
    return _UNKNOWN, lot_str


def filter_for_target(items: list, target_lot: str):
    """统一筛选入口。返回 (有效条目, 排除条目)；
    unknown/_universal/_target 保留，仅 _other 排除。"""
    keep, dropped = [], []
    for it in items:
        scope, _ = classify(read_lot(it), target_lot)
        (dropped if scope == _OTHER else keep).append(it)
    return keep, dropped
