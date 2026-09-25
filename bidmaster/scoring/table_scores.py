"""评分表 → ScoreItem 列表。

表格型评分细则（主流形态）逐行映射；支持"类别行"（技术标（60分））声明类别与总分；
散文型评分细则走 LLM 兜底（scoring/llm_scores.py）。
"""
from __future__ import annotations

import re

from bidmaster.extraction.normalize import normalize_amount
from bidmaster.schemas.document import ParsedDocument, TableModel
from bidmaster.schemas.evidence import EvidenceStore, make_evidence
from bidmaster.schemas.report import AnchorZone
from bidmaster.schemas.scoring import (CAT_COMMERCIAL, CAT_PRICE, CAT_TECHNICAL,
                                       RULE_FORMULA, RULE_GRADED, ScoreItem)

# 类别关键词 → category
_CATEGORY_KW = [(r"技术", CAT_TECHNICAL), (r"商务", CAT_COMMERCIAL),
                (r"价格|报价|评标价", CAT_PRICE)]
_SCORE_CELL_STRICT = re.compile(r"^\s*(\d{1,3}(?:\.\d{1,2})?)\s*分\s*$")
_BARE_NUM = re.compile(r"^\s*(\d{1,3}(?:\.\d{1,2})?)\s*$")
_SCORE_IN_TEXT = re.compile(r"(\d{1,3}(?:\.\d{1,2})?)\s*分")
_SERIAL = re.compile(r"^\d{1,3}$")
_GRADED_KW = re.compile(r"优|良|一般|差|好|中档|较差|档次")
_FORMULA_KW = re.compile(r"公式|基准价|平均值|＝|=")
_PROOF_KW = ["中标通知书", "合同协议书", "合同", "竣工验收证明", "验收证明", "验收报告",
             "证书", "社保证明", "社保", "审计报告", "财务报告", "获奖证书", "承诺函",
             "职称证", "注册证", "考核合格证"]
# "每提供1项...得2分" / "最多6分"
_UNIT_SCORE = re.compile(r"每[^。\n]{0,30}?得\s*(\d+(?:\.\d+)?)\s*分")
_MAX_SCORE = re.compile(r"最多[^0-9]{0,6}(\d+(?:\.\d+)?)\s*分")
_AMOUNT_MENTION = re.compile(r"(?:合同)?金额(?:不低于|达到|在|为)?\s*([\d一二三四五六七八九十佰仟万亿,，.]+\s*万?亿?元?)")
_YEARS_MENTION = re.compile(r"近\s*(\d+)\s*年")
_HEADER_KW = ("评分项", "评分标准", "评分因素", "分值", "分数", "得分", "评分内容", "评审因素")


def _is_score_table(tb) -> bool:
    """表头含评分关键词，或 ≥30% 行含 'N分' 形态。"""
    if not tb.rows:
        return False
    header = "".join(tb.rows[0])
    if any(kw in header for kw in _HEADER_KW):
        return True
    hits = sum(1 for r in tb.rows if any(_SCORE_CELL_STRICT.match(c) or _SCORE_IN_TEXT.search(c) for c in r))
    return hits >= max(2, int(len(tb.rows) * 0.3))


def _detect_category(cell_text: str) -> str | None:
    for pat, cat in _CATEGORY_KW:
        if re.search(pat, cell_text):
            return cat
    return None


def _category_declared_total(cell_text: str) -> float | None:
    m = re.search(r"[（(]?\s*(\d{1,3})\s*分\s*[）)]?", cell_text)
    return float(m.group(1)) if m else None


def extract_score_items(parsed: ParsedDocument, zones: list[AnchorZone],
                        store: EvidenceStore) -> tuple[list[ScoreItem], list[dict]]:
    """返回 (score_items, declared_totals)。

    declared_totals: [{"category": "technical", "total": 60.0, "source": "评分表类别行"}]
    """
    zone_tables = _tables_in_zones(parsed, zones)
    counters = {CAT_TECHNICAL: 0, CAT_COMMERCIAL: 0, CAT_PRICE: 0}
    items: list[ScoreItem] = []
    declared: list[dict] = []

    for tb in zone_tables:
        if not _is_score_table(tb):
            continue
        current_cat: str | None = None
        for ri, row in enumerate(tb.rows):
            cells = [c.strip() for c in row]
            joined = " ".join(c for c in cells if c)
            if not joined:
                continue

            # 类别声明行：技术标（60分）/ 商务标 30分 / 价格标 10分
            # 仅"短单元格含类别词"才认定；声明总分只来自真正的类别行（非空 ≤2 格）
            nonempty = [c for c in cells if c]
            row_cat = None
            for c in cells:
                if not c or len(c) > 14:
                    continue
                cat = _detect_category(c)
                if not cat:
                    continue
                row_cat = cat
                if len(nonempty) <= 2:
                    total = _category_declared_total(c)
                    if total:
                        declared.append({"category": cat, "total": total,
                                         "source": f"{tb.table_id} r{ri}"})
                break
            if row_cat and len(nonempty) <= 2:
                current_cat = row_cat
                continue  # 类别行只做继承，不作为评分项

            # 找分值单元格：显式"N分" → 表头列名指引 → 兜底取末位纯数字（序号在前）
            score, score_ci = _find_score_cell(cells, tb)
            if score is None or score <= 0:
                continue

            # 名称 = 非序号、非分值、最短的有意义单元格；规则 = 最长文本单元格
            name, rule = "", ""
            for ci, c in enumerate(cells):
                if ci == score_ci or not c or _SERIAL.match(c):
                    continue
                if _is_same_number(c, score):
                    continue
                if not name and (1 <= len(c) <= 30):
                    name = c
                elif len(c) > len(rule):
                    rule = c
            if not name:
                name = rule[:20] if rule else f"评分项({tb.table_id} r{ri})"
            if name == rule and rule:
                name = rule[:20]

            cat = current_cat or row_cat or _guess_category(joined)
            counters[cat] += 1
            prefix = {"technical": "T", "commercial": "C", "price": "P"}[cat]
            rule_type = RULE_GRADED if _GRADED_KW.search(rule) else (
                RULE_FORMULA if (cat == CAT_PRICE and _FORMULA_KW.search(rule)) else "binary")
            formula = rule if (cat == CAT_PRICE and _FORMULA_KW.search(rule)) else None

            ev = store.add(make_evidence(
                kind="table_cell", source="table",
                page_no=(tb.page_nos[0] if tb.page_nos else 0),
                table_id=tb.table_id, row=ri,
                snippet=" | ".join(filter(None, cells))[:200]))

            items.append(ScoreItem(
                score_id=f"{prefix}-{counters[cat]:03d}", category=cat, name=name,
                max_score=score, rule_type=rule_type, rule_text=rule[:500],
                parsed_rule=_parse_rule_numbers(rule),
                formula=formula, evidence_required=_find_proofs(rule),
                evidence_ids=[ev.evidence_id], confidence=0.9, status="confirmed",
            ))

    items = _dedupe(items)
    return items, declared


def _tables_in_zones(parsed: ParsedDocument, zones: list[AnchorZone]) -> list[TableModel]:
    """评分/评标办法锚区内的表格；锚区缺失时用全表关键词过滤兜底。"""
    refs: set[str] = set()
    hit_zone = False
    for z in zones:
        if z.zone in ("scoring", "evaluation_method"):
            hit_zone = True
            for b in parsed.blocks[z.block_start:z.block_end]:
                if b.table_ref:
                    refs.add(b.table_ref)
    out = []
    by_id = {t.table_id: t for t in parsed.tables}
    if hit_zone:
        out = [by_id[r] for r in refs if r in by_id]
        if out:
            return out
    # 兜底：全文找"像评分表"的表
    return [t for t in parsed.tables if _is_score_table(t)]


def _find_score_cell(cells: list[str], tb) -> tuple[float | None, int | None]:
    """三段式定位分值单元格，避免把序号列误判为分值。"""
    # pass1: 显式 "N分" 形态
    for ci, c in enumerate(cells):
        m = _SCORE_CELL_STRICT.match(c)
        if m:
            return float(m.group(1)), ci
    # pass2: 表头列名（分值/分数/得分）指引
    header = tb.rows[0] if tb.rows else []
    for ci, c in enumerate(cells):
        if (_BARE_NUM.match(c) and ci < len(header)
                and any(k in header[ci] for k in ("分值", "分数", "得分"))):
            return float(c), ci
    # pass3: 兜底取最后一个纯数字单元格（序号列通常在前，分值列在后）
    found: tuple[float, int] | None = None
    for ci, c in enumerate(cells):
        if ci == 0 or not _BARE_NUM.match(c):
            continue
        found = (float(c), ci)
    return found if found else (None, None)


def _is_same_number(cell: str, score: float) -> bool:
    m = _BARE_NUM.match(cell) or _SCORE_CELL_STRICT.match(cell)
    return bool(m and float(m.group(1)) == score)


def _guess_category(text: str) -> str:
    cat = _detect_category(text)
    return cat or CAT_TECHNICAL


def _parse_rule_numbers(rule: str) -> dict:
    parsed: dict = {}
    m = _UNIT_SCORE.search(rule)
    if m:
        parsed["unit_score"] = float(m.group(1))
    m = _MAX_SCORE.search(rule)
    if m:
        parsed["max_score_cap"] = float(m.group(1))
    m = _YEARS_MENTION.search(rule)
    if m:
        parsed["years"] = int(m.group(1))
    m = _AMOUNT_MENTION.search(rule)
    if m:
        amt = normalize_amount(m.group(1))
        if amt:
            parsed["amount_min"] = amt[0]
    m = re.search(r"(\d+)\s*项", rule)
    if m:
        parsed["count_min"] = int(m.group(1))
    return parsed


def _find_proofs(rule: str) -> list[str]:
    return [kw for kw in _PROOF_KW if kw in rule]


def _dedupe(items: list[ScoreItem]) -> list[ScoreItem]:
    """同名同分的行去重（跨页合并表可能重复）。"""
    seen: set[tuple] = set()
    out = []
    for it in items:
        key = (it.category, it.name, it.max_score)
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out
