"""分值机械校验（宿主做，不给模型判断机会）：
  - 各评分项 max_score > 0
  - 类别内合计 vs 文件声明总分
  - 三大类合计 vs 权重字段 vs 100
不一致 → issues（不会静默丢弃）。
"""
from __future__ import annotations

from bidmaster.schemas.fields import STATUS_FOUND
from bidmaster.schemas.scoring import (CAT_COMMERCIAL, CAT_PRICE, CAT_TECHNICAL,
                                       ScoreItem, ScoreSumCheck)

_CATEGORY_LABEL = {CAT_TECHNICAL: "技术标", CAT_COMMERCIAL: "商务标", CAT_PRICE: "价格标"}


def validate_scores(scores: list, declared: list[dict],
                    field_values: dict[str, str] | None = None,
                    issues: list[str] | None = None) -> list[ScoreSumCheck]:
    issues = issues if issues is not None else []
    # 兼容 dict（阶段产物）与 ScoreItem 两种入参
    from bidmaster.schemas.scoring import ScoreItem as _SI
    scores = [_SI.model_validate(s) if isinstance(s, dict) else s for s in scores]
    checks: list[ScoreSumCheck] = []

    declared_map = {d["category"]: d["total"] for d in declared}
    # 字段抽取到的权重作为声明的第二来源（交叉验证）
    fw = field_values or {}
    field_declared = {
        CAT_TECHNICAL: _to_float(fw.get("technical_weight")),
        CAT_COMMERCIAL: _to_float(fw.get("commercial_weight")),
        CAT_PRICE: _to_float(fw.get("price_weight")),
    }

    grand_computed = 0.0
    grand_declared = 0.0
    for cat in (CAT_TECHNICAL, CAT_COMMERCIAL, CAT_PRICE):
        items = [s for s in scores if s.category == cat]
        computed = round(sum(s.max_score for s in items), 2)
        grand_computed += computed
        dec = declared_map.get(cat)
        fd = field_declared.get(cat)
        if dec is None and fd is not None:
            dec = fd
        if dec is not None:
            grand_declared += dec
        ok = True
        notes: list[str] = []
        if dec is not None and abs(computed - dec) > 0.01:
            ok = False
            notes.append(f"评分项合计 {computed} ≠ 声明总分 {dec}")
        bad = [s.score_id for s in items if s.max_score <= 0]
        if bad:
            ok = False
            notes.append(f"分值异常项: {bad}")
        checks.append(ScoreSumCheck(
            category=cat, declared_total=dec, computed_total=computed,
            item_count=len(items), ok=ok, note="；".join(notes)))

    # 大类合计 = 100（有权重声明时）
    if grand_declared > 0 and abs(grand_declared - 100) > 0.01:
        issues.append(f"评标办法声明的大类权重合计为 {grand_declared}，不等于 100，请人工确认")
    if grand_computed > 0 and grand_declared > 0 and abs(grand_computed - grand_declared) > 0.01:
        issues.append(f"评分项总分合计 {grand_computed} ≠ 声明权重合计 {grand_declared}，"
                      f"可能存在漏行/解析错位（已保留全部条目供人工核对）")
    # 评分项 id 唯一性
    ids = [s.score_id for s in scores]
    if len(ids) != len(set(ids)):
        issues.append("评分项编号存在重复")
    for c in checks:
        if not c.ok and c.note:
            issues.append(f"{_CATEGORY_LABEL[c.category]}：{c.note}")
    return checks


def required_fields_check(fields: dict, issues: list[str]) -> None:
    """必填字段终检：缺失进 issues，不阻塞报告输出。兼容 dict 与 FieldExtraction。"""
    from bidmaster.schemas.fields import FIELD_CATALOG, STATUS_FOUND
    for key, meta in FIELD_CATALOG.items():
        if not meta.get("required") or key not in fields:
            continue
        f = fields[key]
        status = f.get("status") if isinstance(f, dict) else f.status
        if status != STATUS_FOUND:
            issues.append(f"必填字段[{meta['label']}]未提取到，请人工补录")


def _to_float(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
