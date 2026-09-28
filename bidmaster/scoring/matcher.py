"""业绩↔口径保守判定引擎（语义判定第二步，参考 rag-tender 保守判定策略）。

判定纪律：
  1. 要素抽取（电压/设施/工作类型/金额/年份）规则优先，模糊描述才用 LLM 兜底；
  2. 终判由结构化规则执行，LLM 只做要素抽取者、不做裁决者；
  3. 口径要素齐全且全部命中 → match；任一要素明确不符 → no_match；
     要素信息不足 → uncertain（保守，转人工/补材料），绝不硬判。

match_performance(perf_desc, scope, amount_min=None, years=None)
  → {"verdict": match|no_match|uncertain, "elements": {...}, "report": [...], "confidence": float}
"""
from __future__ import annotations

import re
from datetime import datetime

from bidmaster.scoring.scope import extract_scope

_VOLT = re.compile(r"[±+\-]?\s*\d{3,4}\s*(?:kV|KV|kv|千伏)")
_YEAR = re.compile(r"(?:19|20)\d{2}")
_AMOUNT = re.compile(r"(\d[\d,，]*(?:\.\d+)?)\s*(万元|亿元|万|亿|元)")


def _norm_volt(v: str) -> str:
    return re.sub(r"[±+\-\s]", "", v).replace("千伏", "kV").upper()


def extract_perf_elements(desc: str) -> dict:
    """从业绩描述中抽取可比对要素（与 scope 同维度）。"""
    els: dict = {}
    volts = sorted({_norm_volt(m.group(0)) for m in _VOLT.finditer(desc)})
    if volts:
        els["voltage_levels"] = volts
    from bidmaster.scoring.scope import _FACILITY_KW, _WORK_KW, _SUBPKG
    facs = [kw for kw in _FACILITY_KW if kw in desc]
    if facs:
        els["facilities"] = facs
    works = [kw for kw in _WORK_KW if kw in desc]
    if works:
        els["work_types"] = works
    m = _SUBPKG.search(desc)
    if m:
        els["subdivision"] = m.group(0)
    m = _AMOUNT.search(desc)
    if m:
        val = float(m.group(1).replace(",", "").replace("，", ""))
        unit = m.group(2)
        if unit.startswith("亿"):
            val *= 1e8
        elif unit.startswith("万"):
            val *= 1e4
        els["amount"] = val
    y = _YEAR.findall(desc)
    if y:
        els["years_mentioned"] = sorted({int(x) for x in y})
    return els


def match_performance(perf_desc: str, scope: dict, *,
                      amount_min: float | None = None,
                      years: int | None = None,
                      llm_elements: dict | None = None) -> dict:
    """保守判定：scope 为口径结构化要素（extract_scope 产物）。

    llm_elements：模糊业绩描述时由 LLM 抽取的要素补充（可选）。
    """
    els = extract_perf_elements(perf_desc)
    if llm_elements:
        for k, v in llm_elements.items():
            if v:
                els[k] = v  # LLM 理解否定/细分语义，覆盖规则抽取
        negs = llm_elements.get("negations") or []
        wt = els.get("work_types")
        if wt and negs:
            els["work_types"] = [w for w in wt if not any(n in w or w in n for n in negs)]

    report: list[dict] = []
    has_miss = False
    has_unknown = False
    known_dims = 0

    def _add(dim, state, detail):
        nonlocal has_miss, has_unknown, known_dims
        report.append({"dim": dim, "state": state, "detail": detail})
        if state == "miss":
            has_miss = True
            known_dims += 1
        elif state == "hit":
            known_dims += 1
        else:
            has_unknown = True

    # 电压等级：口径有要求 → 业绩必须出现同等级之一
    sv = scope.get("voltage_levels")
    if sv:
        pe = els.get("voltage_levels")
        if not pe:
            _add("voltage_levels", "unknown", "业绩描述未提及电压等级")
        elif set(pe) & set(sv):
            _add("voltage_levels", "hit", f"命中 {sorted(set(pe) & set(sv))}")
        else:
            _add("voltage_levels", "miss", f"口径要求 {sv}，业绩为 {pe}")

    # 设施类型：口径有 → 业绩须含同类型（业绩含其他设施类型 → miss）
    sf = scope.get("facilities")
    if sf:
        pe = els.get("facilities")
        if not pe:
            _add("facilities", "unknown", "业绩描述未提及设施类型")
        elif set(pe) & set(sf):
            _add("facilities", "hit", f"命中 {sorted(set(pe) & set(sf))}")
        else:
            _add("facilities", "miss", f"口径要求 {sf}，业绩为 {pe}")

    # 工作类型：口径有 → 业绩须含同类型
    sw = scope.get("work_types")
    if sw:
        pe = els.get("work_types")
        if not pe:
            _add("work_types", "unknown", "业绩描述未提及工作类型")
        elif set(pe) & set(sw):
            _add("work_types", "hit", f"命中 {sorted(set(pe) & set(sw))}")
        else:
            _add("work_types", "miss", f"口径要求 {sw}，业绩为 {pe}")

    # 标包细分：口径限定（如 A包）→ 业绩明示其他包 → miss；未提及 → unknown
    sub = scope.get("subdivision")
    if sub:
        pe = els.get("subdivision")
        if pe and pe != sub:
            _add("subdivision", "miss", f"口径限定 {sub}，业绩为 {pe}")
        else:
            _add("subdivision", "hit", sub)

    # 金额门槛
    if amount_min:
        pa = els.get("amount")
        if pa is None:
            _add("amount", "unknown", "业绩描述未提及合同金额")
        elif pa >= amount_min:
            _add("amount", "hit", f"{pa:g} 元 ≥ 门槛 {amount_min:g} 元")
        else:
            _add("amount", "miss", f"{pa:g} 元 < 门槛 {amount_min:g} 元")

    # 年份窗口
    if years:
        ym = els.get("years_mentioned")
        if not ym:
            _add("years", "unknown", "业绩描述未提及年份")
        else:
            import datetime as _dt
            now = _dt.datetime.now().year
            recent = [y for y in ym if y >= now - years - 1]  # 竣工在窗口内的近似判定
            if recent:
                _add("years", "hit", f"{recent} 在近 {years} 年窗口内")
            else:
                _add("years", "miss", f"年份 {ym} 超出近 {years} 年窗口")

    # 裁决：有 miss → no_match；未知占比高 → uncertain；全命中 → match
    if has_miss:
        verdict = "no_match"
    elif has_unknown:
        verdict = "uncertain"
    else:
        verdict = "match" if known_dims else "uncertain"

    conf = round(0.6 + 0.1 * min(known_dims, 3) - (0.15 if verdict == "uncertain" else 0), 2)
    conf = max(conf, 0.5) if known_dims else 0.5
    return {"verdict": verdict, "elements": els, "report": report,
            "confidence": max(min(conf, 0.9), 0.5)}
