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
# 包号列表分隔符（顿号/逗号/分号/空白）
_LIST_SEP_RE = re.compile(r"[、,，;；\s]+")
# 包号区间："包1-包3" / "包1~包3" / "包1至包3"（可出现多个，全部收集）
_RANGE_RE = re.compile(r"包\s*0*(\d+)\s*[-~～至到]\s*(?:包\s*)?0*(\d+)")


def _full2half(s: str) -> str:
    table = str.maketrans("０１２３４５６７８９", "0123456789")
    return (s or "").translate(table)


def package_numbers(lot: str | None) -> set[int]:
    """兼容入口：只返回包号集合。需要"解析是否完整"信息时用 parse_packages()。"""
    return parse_packages(lot)[0]


def parse_packages(lot: str | None) -> tuple[set[int], bool]:
    """解析包号表达式 → (包号集合, 是否完整解析)。

    区间语法在空白拆分**之前**于原串扫描（"包1 至 包3"、"监理包1-包3" 均可
    完整识别为区间 {1,2,3}，不会被拆碎成 {1,3}）；其余按分隔符切分后
    逐段匹配 包N / 第N包 / N号包 / 标段N / 共享前缀裸数字（"包1、2、3"）。
    存在带数字但无法归类的片段 → complete=False——调用方不得据此排除评分
    （如"500千伏线路工程"：数字是电压等级 → 待确认保留）。
    """
    s = _full2half(lot)
    if not s:
        return set(), True
    nums: set[int] = set()
    complete = True
    remainder: list[str] = []
    last = 0
    # 区间优先：原串直接扫描，容忍区间内空白（"包1 至 包3"）
    for m in _RANGE_RE.finditer(s):
        remainder.append(s[last:m.start()])
        lo, hi = int(m.group(1)), int(m.group(2))
        if hi >= lo:
            nums.update(range(lo, hi + 1))
        else:
            complete = False
        last = m.end()
    remainder.append(s[last:])
    rem = " ".join(remainder)
    has_pkg_prefix = bool(nums)  # 已有区间 → 后续裸数字属于同一列表
    segments = [seg for seg in _LIST_SEP_RE.split(rem) if seg]
    for seg in segments:
        seg = seg.strip()
        m = _RANGE_RE.fullmatch(seg)
        if m:
            lo, hi = int(m.group(1)), int(m.group(2))
            if hi >= lo:
                nums.update(range(lo, hi + 1))
            else:
                complete = False
            has_pkg_prefix = True
            continue
        matched = False
        for pat in _PKG_PATTERNS:
            mm = pat.fullmatch(seg)
            if mm:
                nums.add(int(mm.group(1)))
                matched = True
                has_pkg_prefix = True
                break
        if matched:
            continue
        md = re.fullmatch(r"0*(\d+)", seg)
        if md and has_pkg_prefix:
            nums.add(int(md.group(1)))  # 共享前缀列表：包1、2、3 / 包1-包3、5
            continue
        if re.search(r"\d", seg):
            complete = False  # 带数字但无法归类（可能是没写全的列表项）
    return nums, complete


def search_numbers(lot: str | None) -> set[int]:
    """搜索式提取：全文扫描显式包号语法（包N/第N包/N号包/标段N），
    不要求整段匹配。用于表达式解析不完整时的回退——仍只认"包"类关键词，
    电压等级（500千伏）、年份（2026年度）不含包号语法，安全返回空集。"""
    s = _full2half(lot)
    if not s:
        return set()
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


def split_fenbiao(lot: str | None) -> tuple[str, str]:
    """复合键 "分标::包号" 拆分；非复合键返回 ("", 原值)。"""
    s = lot or ""
    if "::" in s:
        fb, bare = s.split("::", 1)
        return fb.strip(), bare.strip()
    return "", s.strip()


def _embedded_pkg_num(name: str) -> int | None:
    """名称内嵌包号宽松提取（"结算审核包1"/"监理包12" → 1/12）。
    用于包名称匹配；范围表达式请用 parse_packages（严格语法+完整性）。"""
    m = re.search(r"包\s*0*(\d+)", _full2half(name or ""))
    return int(m.group(1)) if m else None


def same_lot(a: str, b: str) -> bool:
    """同包判定（名称语义）：分标一致（双方都无分标视为一致）且内嵌包号相等。
    分标明确冲突 → False。缺分标依据的场景请走 classify（返回待确认）。"""
    if not a or not b:
        return False
    if a == b:
        return True
    fa, ba = split_fenbiao(a)
    fb_, bb = split_fenbiao(b)
    if (fa or fb_) and fa != fb_:
        return False
    na, nb = _embedded_pkg_num(ba), _embedded_pkg_num(bb)
    return na is not None and nb is not None and na == nb


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
        # 覆盖多包的表达式（"包1、2、3"/"包1-包3"）→ 通用；单一包 → 目标包
        l_nums, l_complete = parse_packages(ba)
        if l_complete and len(l_nums) > 1:
            return _UNIVERSAL, f"通用·含目标包（{lot_str[:14]}）"
        return _TARGET, "目标包"
    if fa and fbt and fa != fbt:
        # 双方分标明确不同：即使包号数字相同也不是同一个包
        return _OTHER, f"其他分标·{lot_str[:12]}"
    if (fa or fbt) and fa != fbt:
        # 仅一方有分标：归属依据不足，即便包号无交集也不据此排除
        return _UNKNOWN, f"待确认·{lot_str[:16]}"
    # 范围表达式严格解析；不完整时搜索式提取只能提供"命中目标包"的正向证据，
    # 不能作为排除依据——搜索不展开区间语义（"监理包1-包3"只会搜到 1 和 3）
    l_nums, l_complete = parse_packages(ba)
    t_num = _embedded_pkg_num(bt)  # 目标是包名称 → 宽松内嵌提取
    if t_num is None:
        return _UNKNOWN, f"待确认·{lot_str[:16]}"
    if not l_nums:
        # 未提取到任何包号语法（如纯文字提示"换流站土建施工"）→ 待确认
        return _UNKNOWN, f"待确认·{lot_str[:16]}"
    if t_num in l_nums:  # 严格完整解析命中 → 强证据
        if len(l_nums) > 1:
            return _UNIVERSAL, f"通用·含目标包（{lot_str[:14]}）"
        return _TARGET, "目标包"
    if l_complete:
        # 完整解析且目标包号不在集合中 → 明确其他包（唯一可排除）
        return _OTHER, f"其他包·{ba[:14]}"
    if t_num in search_numbers(ba):
        # 弱证据命中：可确认适用，但范围可能未读全（不标待确认以外的排除）
        return _UNIVERSAL, f"通用·含目标包（{lot_str[:14]}）"
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
