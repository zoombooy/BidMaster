"""评分表 → ScoreItem 列表。支持两种主流形态：

1. 经典格式：| 序号 | 评分项 | 分值 | 评分标准 |（政府/房建市政类）
2. 国网格式：| 序号 | 项目(大类+分值) | 评审内容及分值(子项+分值) | 项目内容(梯度细则) |
   ——分值嵌在单元格文字里（"信誉评价（18分）"），同一子项可能拆多行细分为 2分+3分；
   技术标按分标各一张表（lot 维度）；类别由所在章节标题决定（前附表之三：商务评分标准）。

类别优先级：所在章节标题 > 行内类别词 > 关键词猜测。
"""
from __future__ import annotations

import re

from bidmaster.extraction.normalize import normalize_amount
from bidmaster.schemas.document import ParsedDocument, TableModel
from bidmaster.schemas.evidence import EvidenceStore, make_evidence
from bidmaster.schemas.report import AnchorZone
from bidmaster.schemas.scoring import (CAT_COMMERCIAL, CAT_PRICE, CAT_TECHNICAL,
                                       RULE_FORMULA, RULE_GRADED, ScoreItem)

_CATEGORY_KW = [(r"技术", CAT_TECHNICAL), (r"商务", CAT_COMMERCIAL),
                (r"价格|报价|评标价", CAT_PRICE)]
_SCORE_CELL_STRICT = re.compile(r"^\s*(\d{1,3}(?:\.\d{1,2})?)\s*分\s*$")
_BARE_NUM = re.compile(r"^\s*(\d{1,3}(?:\.\d{1,2})?)\s*$")
_PAREN_SCORE = re.compile(r"[（(]\s*(-?\d{1,3}(?:\.\d{1,2})?)\s*分\s*[）)]")
_SCORE_IN_TEXT = re.compile(r"(\d{1,3}(?:\.\d{1,2})?)\s*分")
_SERIAL = re.compile(r"^(?:[一二三四五六七八九十]{1,3}|\d{1,3})$")
_GRADED_KW = re.compile(r"优|良|一般|差|好|中档|较差|档次")
_FORMULA_KW = re.compile(r"公式|基准价|平均值|＝|=")
_PROOF_KW = ["中标通知书", "合同协议书", "合同", "竣工验收证明", "验收证明", "验收报告",
             "证书", "社保证明", "社保", "审计报告", "财务报告", "获奖证书", "承诺函",
             "职称证", "注册证", "考核合格证"]
_HEADER_KW = ("评分项", "评分标准", "评分因素", "分值", "分数", "得分", "评分内容", "评审因素")
# 国网格式表头特征
_GRID_HEADER_KW = ("评审内容及分值", "项目内容")
_UNIT_SCORE = re.compile(r"每[^。\n]{0,30}?得\s*(\d+(?:\.\d+)?)\s*分")
_MAX_SCORE = re.compile(r"最多[^0-9]{0,6}(\d+(?:\.\d+)?)\s*分")
_AMOUNT_MENTION = re.compile(r"(?:合同)?金额(?:不低于|达到|在|为)?\s*([\d一二三四五六七八九十佰仟万亿,，.]+\s*万?亿?元?)")
_YEARS_MENTION = re.compile(r"近\s*(\d+)\s*年")
# 权重表/否决表等非评分表的表头特征 → 排除
_NOT_SCORE_HEADER = ("权重设置", "情形事项", "评审因素", "条款号", "奖项", "工程量清单")


def _category_from_text(text: str) -> str | None:
    for pat, cat in _CATEGORY_KW:
        if re.search(pat, text):
            return cat
    return None


def _category_from_sections(sections: list, block_idx: int) -> str | None:
    """表格所在的最内层章节标题决定类别（商务评分标准/技术评分标准/价格评分标准）。"""
    best = None  # (level, title)
    for sec in sections:
        start = sec.block_start if hasattr(sec, "block_start") else sec.get("block_start")
        end = sec.block_end if hasattr(sec, "block_end") else sec.get("block_end")
        level = sec.level if hasattr(sec, "level") else sec.get("level", 0)
        title = sec.title if hasattr(sec, "title") else sec.get("title", "")
        if start <= block_idx < end and (best is None or level > best[0]):
            best = (level, title)
    if best:
        return _category_from_text(best[1])
    return None


def _is_score_table(tb: TableModel) -> bool:
    if not tb.rows:
        return False
    header = "".join(tb.rows[0])
    if any(kw in header for kw in _NOT_SCORE_HEADER):
        return False
    if any(kw in header for kw in _HEADER_KW):
        return True
    if any(kw in header for kw in _GRID_HEADER_KW):
        return True
    hits = sum(1 for r in tb.rows
               if any(_SCORE_CELL_STRICT.match(c) or _SCORE_IN_TEXT.search(c) for c in r))
    return hits >= max(2, int(len(tb.rows) * 0.3))


def _is_grid_table(tb: TableModel) -> bool:
    header = "".join(tb.rows[0]) if tb.rows else ""
    return any(kw in header for kw in _GRID_HEADER_KW) and len(tb.rows) > 2


def _collect_zone_tables(parsed: ParsedDocument, zones: list[AnchorZone]):
    """全文收集评分表 + lot 提示（表前最近的短段落，如"换流站土建施工"）。

    不依赖锚区边界——真实招标文件中"注：xxx"等段落常被误判为高级别标题，
    把锚区截断导致漏表。锚区仅用于排序加分（zone 内的表排前面）。
    """
    zone_refs: set[str] = set()
    for z in zones or []:
        if z.zone in ("scoring", "evaluation_method"):
            for b in parsed.blocks[z.block_start:z.block_end]:
                if b.table_ref:
                    zone_refs.add(b.table_ref)

    all_refs: list[str] = []
    hints: dict[str, str] = {}
    last_short = ""
    for b in parsed.blocks:
        text = (b.text or "").strip()
        if b.table_ref:
            if b.table_ref not in hints:
                all_refs.append(b.table_ref)
                hints[b.table_ref] = last_short
        elif (b.type != "heading" and text and len(text) <= 20
              and not text.endswith(("：", ":"))):
            last_short = text

    ordered = ([r for r in all_refs if r in zone_refs]
               + [r for r in all_refs if r not in zone_refs])
    return ordered, hints


def extract_score_items(parsed: ParsedDocument, zones: list[AnchorZone],
                        store: EvidenceStore, sections: list | None = None
                        ) -> tuple[list[ScoreItem], list[dict]]:
    counters = {CAT_TECHNICAL: 0, CAT_COMMERCIAL: 0, CAT_PRICE: 0}
    items: list[ScoreItem] = []
    declared: list[dict] = []
    by_id = {t.table_id: t for t in parsed.tables}
    sections = sections or []

    refs_list, refs_hint = _collect_zone_tables(parsed, zones)
    if not refs_list:
        return items, declared

    for tid in refs_list:
        tb = by_id[tid]
        if not _is_score_table(tb):
            continue
        block_idx = next((i for i, b in enumerate(parsed.blocks)
                          if b.table_ref == tid), 0)
        cat = (_category_from_sections(sections, block_idx)
               or _category_from_table(tb))
        lot = refs_hint.get(tid, "")
        if _is_grid_table(tb):
            _parse_grid_table(tb, cat, lot, store, counters, items, declared)
        else:
            _parse_classic_table(tb, cat, lot, store, counters, items, declared)

    items = _dedupe(items)
    return items, declared


def _category_from_table(tb: TableModel) -> str:
    joined = " ".join("".join(r) for r in tb.rows[:3])
    return _category_from_text(joined) or CAT_TECHNICAL


# ---------------- 国网格式 ----------------
def _parse_grid_table(tb: TableModel, category: str, lot: str,
                      store: EvidenceStore, counters: dict,
                      items: list, declared: list) -> None:
    ncols = max(len(r) for r in tb.rows)
    groups: dict[tuple, dict] = {}
    order: list[tuple] = []
    last_name = ""
    parent_declared: dict[str, float] = {}

    for ri, row in enumerate(tb.rows[1:], 1):
        cells = list(row) + [""] * (ncols - len(row))
        parent = cells[1].strip() if ncols > 1 else ""
        name = cells[2].strip() if ncols > 2 else ""
        content = cells[3].strip() if ncols > 3 else ""
        if not parent and not name and not content:
            continue
        if not name:
            name = last_name
        last_name = name

        m = _PAREN_SCORE.search(name) or _PAREN_SCORE.search(parent)
        score = float(m.group(1)) if m else None
        penalty = bool(m and m.group(1).startswith("-"))
        # "（-20-0）"/"扣分"等扣分形态（不在"（-N分）"标准形里）
        if not penalty and re.search(r"[（(]\s*-\d+|扣分", name + " " + parent):
            penalty = True
            if score is None:
                m2 = _SCORE_IN_TEXT.search(content[:40])
                if m2:
                    score = float(m2.group(1))
        if penalty and score is not None:
            score = abs(score)
        # 大类声明分（同一父类只记一次）
        pm = _PAREN_SCORE.search(parent)
        if pm and not pm.group(1).startswith("-") and parent not in parent_declared:
            parent_declared[parent] = float(pm.group(1))

        key = (parent, name)
        if key in groups:
            groups[key]["texts"].append(content)
            groups[key]["penalty"] = groups[key]["penalty"] or penalty
        else:
            groups[key] = {"score": score, "texts": [content], "row": ri,
                           "penalty": penalty}
            order.append(key)

    for key in order:
        parent, name = key
        g = groups[key]
        score = g["score"]
        rule = " / ".join(t for t in g["texts"] if t)[:800]
        if score is None:
            m = _SCORE_IN_TEXT.search(rule[:60])
            if m:
                score = float(m.group(1))
        if not score or score <= 0:
            continue
        counters[category] += 1
        prefix = {"technical": "T", "commercial": "C", "price": "P"}[category]
        rule_type = RULE_GRADED if _GRADED_KW.search(rule) else (
            RULE_FORMULA if (category == CAT_PRICE and _FORMULA_KW.search(rule)) else "binary")
        ev = store.add(make_evidence(
            kind="table_cell", source="table",
            page_no=(tb.page_nos[0] if tb.page_nos else 0),
            table_id=tb.table_id, row=g["row"],
            snippet=f"{parent} | {name} | {rule[:120]}"))
        parsed_rule = _parse_rule_numbers(rule)
        parsed_rule["group"] = re.sub(r"\s*[（(]-?\d+(?:\.\d+)?分[）)]\s*", "", parent)
        if lot:
            parsed_rule["lot"] = lot
        if g.get("penalty"):
            parsed_rule["penalty"] = True
        items.append(ScoreItem(
            score_id=f"{prefix}-{counters[category]:03d}", category=category,
            name=re.sub(r"\s*[（(]-?\d+(?:\.\d+)?分[）)]\s*", "", name)[:40],
            max_score=score, rule_type=rule_type, rule_text=rule[:500],
            parsed_rule=parsed_rule,
            formula=rule if (category == CAT_PRICE and _FORMULA_KW.search(rule)) else None,
            evidence_required=_find_proofs(rule),
            evidence_ids=[ev.evidence_id],
            confidence=0.75 if penalty else 0.9, status="confirmed"))

    # 声明总分 = 大类标签合计（表内自洽校验基准）
    if parent_declared:
        declared.append({"category": category,
                         "total": round(sum(parent_declared.values()), 2),
                         "source": f"{tb.table_id} 大类标签",
                         "lot": lot or "-"})


# ---------------- 经典格式 ----------------
def _parse_classic_table(tb: TableModel, default_cat: str, lot: str,
                         store: EvidenceStore, counters: dict,
                         items: list, declared: list) -> None:
    current_cat = None
    for ri, row in enumerate(tb.rows):
        cells = [c.strip() for c in row]
        joined = " ".join(c for c in cells if c)
        if not joined:
            continue

        # 类别声明行：技术标（60分）——仅"短单元格含类别词"才认定
        nonempty = [c for c in cells if c]
        row_cat = None
        for c in cells:
            if not c or len(c) > 14:
                continue
            cat = _category_from_text(c)
            if not cat:
                continue
            row_cat = cat
            if len(nonempty) <= 2:
                m = re.search(r"[（(]?\s*(\d{1,3})\s*分\s*[）)]?", c)
                if m:
                    declared.append({"category": cat, "total": float(m.group(1)),
                                     "source": f"{tb.table_id} r{ri}",
                                     "lot": lot or "-"})
            break
        if row_cat:
            current_cat = row_cat
            if len(nonempty) <= 2:
                continue  # 类别行只做继承，不作为评分项

        score, score_ci = _find_score_cell(cells, tb)
        # 价格公式 blob（如"2.2.4（3）投标报价评分标准"整段文本）→ 公式条目
        if score is None and joined and ("投标报价评分标准" in joined
                                        or "评标价格计算公式" in joined
                                        or "价格评分标准" in joined):
            counters[CAT_PRICE] += 1
            ev = store.add(make_evidence(
                kind="table_cell", source="table",
                page_no=(tb.page_nos[0] if tb.page_nos else 0),
                table_id=tb.table_id, row=ri, snippet=joined[:200]))
            items.append(ScoreItem(
                score_id=f"P-{counters[CAT_PRICE]:03d}", category=CAT_PRICE,
                name="投标报价评分", max_score=0.0, rule_type=RULE_FORMULA,
                rule_text=joined[:500], parsed_rule={"formula_blob": True},
                formula=joined[:500], evidence_ids=[ev.evidence_id],
                confidence=0.7, status="pending_review"))
            continue
        if score is None or score <= 0:
            continue

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

        cat = current_cat or row_cat or default_cat
        counters[cat] += 1
        prefix = {"technical": "T", "commercial": "C", "price": "P"}[cat]
        rule_type = RULE_GRADED if _GRADED_KW.search(rule) else (
            RULE_FORMULA if (cat == CAT_PRICE and _FORMULA_KW.search(rule)) else "binary")
        ev = store.add(make_evidence(
            kind="table_cell", source="table",
            page_no=(tb.page_nos[0] if tb.page_nos else 0),
            table_id=tb.table_id, row=ri,
            snippet=" | ".join(filter(None, cells))[:200]))
        parsed_rule = _parse_rule_numbers(rule)
        if lot:
            parsed_rule["lot"] = lot
        items.append(ScoreItem(
            score_id=f"{prefix}-{counters[cat]:03d}", category=cat, name=name[:40],
            max_score=score, rule_type=rule_type, rule_text=rule[:500],
            parsed_rule=parsed_rule,
            formula=rule if (cat == CAT_PRICE and _FORMULA_KW.search(rule)) else None,
            evidence_required=_find_proofs(rule),
            evidence_ids=[ev.evidence_id], confidence=0.9, status="confirmed"))


# ---------------- 公共工具 ----------------
def _find_score_cell(cells: list[str], tb) -> tuple[float | None, int | None]:
    """三段式定位分值单元格，避免把序号列误判为分值。"""
    for ci, c in enumerate(cells):
        m = _SCORE_CELL_STRICT.match(c)
        if m:
            return float(m.group(1)), ci
    header = tb.rows[0] if tb.rows else []
    for ci, c in enumerate(cells):
        if (_BARE_NUM.match(c) and ci < len(header)
                and any(k in header[ci] for k in ("分值", "分数", "得分"))):
            return float(c), ci
    found: tuple[float, int] | None = None
    for ci, c in enumerate(cells):
        if ci == 0 or not _BARE_NUM.match(c):
            continue
        found = (float(c), ci)
    return found if found else (None, None)


def _is_same_number(cell: str, score: float) -> bool:
    m = _BARE_NUM.match(cell) or _SCORE_CELL_STRICT.match(cell)
    return bool(m and float(m.group(1)) == score)


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
    """同类别同名同分去重（跨页合并表可能重复；不同 lot 保留）。"""
    seen: set[tuple] = set()
    out = []
    for it in items:
        key = (it.category, it.name, it.max_score,
               (it.parsed_rule or {}).get("lot", ""))
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out
