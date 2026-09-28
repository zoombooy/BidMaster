""""同类工程"口径结构化抽取（语义判定第一步）。

从评分细则/资格条款原文中抽取判定口径的结构化要素：
  voltage_levels 电压等级（±800kV/1000kV…）
  facilities      设施类型（换流站/变电站/线路…）
  work_types      工作类型（土建/电气安装/监理/结算审核…）
  subdivision     标包细分（A包/包12-15…）
  definition      文件对"同类工程"的明确定义句（如有）
  amount_min / years 等量化要素由 requirement 模块负责，此处不重复。

判定原则（rag-tender 保守判定）：口径要素齐全 → 结构化硬判；
要素缺失 → 匹配结果标 uncertain，不硬判。
"""
from __future__ import annotations

import re

# 电压等级：±800kV / 1000kV / 500千伏
_VOLT = re.compile(r"[±+\-]?\s*\d{3,4}\s*(?:kV|KV|kv|千伏)")
# 设施类型词典（按长度优先匹配）
_FACILITY_KW = ["换流站", "变电站", "开关站", "换流站阀厅", "GIL管廊", "线路",
                "火电厂", "风电场", "光伏电站", "配电网", "电缆隧道"]
# 工作类型词典
_WORK_KW = ["房屋建筑", "土建", "电气安装", "监理", "监造", "系统调试", "结算审核", "决算审核",
            "造价咨询", "桩基", "施工", "设计", "科研", "工程保险", "技术服务",
            "检修", "改造", "新建"]
# 标包细分
_SUBPKG = re.compile(r"[A-Z]包|包\d+(?:-\d+)?(?:包)?|第[一二三四五六七八九十]+包")
# 明确定义句："同类工程是指/指的是..."
_DEF_RE = re.compile(r"(?:同类|类似)(?:工程|项目|业绩)(?:是指|指的是|指|系指)([^。；\n]{4,80})")

_VOLT_NORM_RE = re.compile(r"[±+\-\s]")


def _norm_volt(v: str) -> str:
    return _VOLT_NORM_RE.sub("", v).replace("千伏", "kV").upper()


def extract_scope(text: str) -> dict:
    """从口径原文抽取结构化 scope；无任何要素返回空 dict。"""
    scope: dict = {}

    volts = sorted({_norm_volt(m.group(0)) for m in _VOLT.finditer(text)})
    if volts:
        scope["voltage_levels"] = volts

    facs = [kw for kw in _FACILITY_KW if kw in text]
    if facs:
        scope["facilities"] = facs

    works = [kw for kw in _WORK_KW if kw in text]
    if works:
        scope["work_types"] = works

    m = _SUBPKG.search(text)
    if m:
        scope["subdivision"] = m.group(0)

    m = _DEF_RE.search(text)
    if m:
        scope["definition"] = m.group(1).strip()

    return scope
