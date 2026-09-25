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
    """分值机械校验。多分标文件（同一类别多张评分表）按 lot 分组校验：
    每个分标内部"评分项合计 == 该表大类标签合计"；权重字段作为参考线单列。"""
    issues = issues if issues is not None else []
    from bidmaster.schemas.scoring import ScoreItem as _SI
    scores = [_SI.model_validate(s) if isinstance(s, dict) else s for s in scores]
    checks: list[ScoreSumCheck] = []

    def lot_of(s: ScoreItem) -> str:
        return (s.parsed_rule or {}).get("lot", "-")

    # ---- 分组1：按 (category, lot) 校验该分标内部自洽（大类标签 vs 子项合计）----
    lots = sorted({(s.category, lot_of(s)) for s in scores})
    for cat, lot in lots:
        group = [s for s in scores if s.category == cat and lot_of(s) == lot]
        group_declared = [d for d in declared
                          if d.get("category") == cat and d.get("lot", "-") == lot]
        computed = round(sum(s.max_score for s in group if s.status == "confirmed"
                             and not (s.parsed_rule or {}).get("penalty")), 2)
        dec = group_declared[0]["total"] if group_declared else None
        ok = True
        notes: list[str] = []
        if dec is not None and abs(computed - dec) > 0.01:
            ok = False
            notes.append(f"评分项合计 {computed} ≠ 大类标签合计 {dec}")
        bad = [s.score_id for s in group
               if s.max_score <= 0 and s.status == "confirmed"]
        if bad:
            ok = False
            notes.append(f"分值异常项: {bad}")
        label = f"{_CATEGORY_LABEL.get(cat, cat)}/{lot}"
        checks.append(ScoreSumCheck(
            category=cat, declared_total=dec, computed_total=computed,
            item_count=len(group), ok=ok,
            note=("；".join(notes) if notes else "") + (f" [lot={lot}]" if lot else "")))
        if not ok and notes:
            issues.append(f"{label}：{'；'.join(notes)}")

    # ---- 分组2：权重字段（技:商:价）合计参考线 ----
    fw = field_values or {}
    weights = {CAT_TECHNICAL: _to_float(fw.get("technical_weight")),
               CAT_COMMERCIAL: _to_float(fw.get("commercial_weight")),
               CAT_PRICE: _to_float(fw.get("price_weight"))}
    if any(v is not None for v in weights.values()):
        grand_w = sum(v for v in weights.values() if v is not None)
        if abs(grand_w - 100) > 0.01:
            issues.append(f"权重（技:商:价）合计 {grand_w} ≠ 100，可能取到了其他分标的行，请人工确认")
    # 评分项 id 唯一性
    ids = [s.score_id for s in scores]
    if len(ids) != len(set(ids)):
        issues.append("评分项编号存在重复")
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
