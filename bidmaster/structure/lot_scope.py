"""分标/包件作用域识别与包件级字段抽取（P0 范围解析）。

对标 bid-agent-vscode 处理账本的对象归属思想 + BidPilot-AI 的 scope 模型：
  文件级：从文件名/文件开头内容识别"这个文件属于哪个分标/包件"
  表格级：公告的逐包限价表（包号/工程名称/包名称/…/分项限价/最高限价）
          与包件文件的工期表（标包划分/…/开始时间/完成时间）按行解析，
          产出 per-lot 字段——多分标公告不再共用一个"最高限价"。
"""
from __future__ import annotations

import re

from bidmaster.schemas.document import ParsedDocument
from bidmaster.schemas.fields import FieldCandidate, FieldExtraction, empty_extraction
from bidmaster.schemas.evidence import EvidenceStore, make_evidence

# 文件名/标题中的包件名：结算审核包1 / 监造包2 / 包12-15 / 施工A包
_LOT_NAME_RE = re.compile(
    r"(?:结算审核|竣工决算审核|监造|监理|系统调试|科研|工程保险|施工|设计)?包\d{1,2}(?:-\d{1,2})?")

_DATE = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月")


def infer_file_scope(file_name: str, blocks: list) -> dict | None:
    """识别一个源文件属于哪个包件。

    优先文件名（结算审核包1新.docx → 结算审核包1），
    其次文件开头的标题块（"浙江特高压交流环网线路工程结算审核包1"）。
    公告/通用文件 → None（全局作用域）。
    """
    m = _LOT_NAME_RE.search(file_name)
    lot_from_name = m.group(0) if m else None
    # 内容扫描：标题块找包件名与其前缀（= 项目/工程名）
    seen = 0
    for b in blocks[:12]:
        t = (b.text or "").strip()
        if not t or t.startswith("[文件"):
            continue  # 跳过合并标记块（其文本含文件名，会提前误匹配且无项目前缀）
        seen += 1
        m = _LOT_NAME_RE.search(t)
        if m and seen <= 6:
            lot = m.group(0)
            proj = t[:m.start()].strip(" 　-—")
            if lot_from_name and _norm_lot(lot) != _norm_lot(lot_from_name):
                continue  # 标题里的包件与文件名不一致，继续找
            return {"lot": lot_from_name or lot,
                    "project": proj if len(proj) >= 6 else ""}
    if lot_from_name:
        return {"lot": lot_from_name, "project": ""}
    return None


def _norm_lot(name: str) -> str:
    return re.sub(r"[ \t]", "", name)


def _find_col(header_cells: list[str], keywords: tuple[str, ...]) -> int | None:
    for ci, h in enumerate(header_cells):
        if any(kw in h for kw in keywords):
            return ci
    return None


def extract_lot_table_fields(parsed: ParsedDocument,
                             store: EvidenceStore) -> dict[str, dict[str, FieldExtraction]]:
    """从公告逐包限价表 / 包件工期表按行解析 per-lot 字段。

    返回 {lot_name: {field_key: FieldExtraction}}，lot 名做归一化关联。
    """
    lot_fields: dict[str, dict[str, FieldExtraction]] = {}

    def _put(lot: str, key: str, value_raw: str, value_norm: str | None,
             conf: float, ev) -> None:
        lot = _norm_lot(lot)
        bucket = lot_fields.setdefault(lot, {})
        f = bucket.get(key) or empty_extraction(key)
        cand = FieldCandidate(value_raw=value_raw, value_normalized=value_norm or "",
                              source="table", confidence=conf,
                              evidence_id=ev.evidence_id, zone="包件表")
        f.candidates.append(cand)
        if f.status != "found" or conf >= f.confidence:
            f.status = "found"
            f.value_raw = value_raw
            f.value_normalized = value_norm
            f.confidence = conf
            f.evidence_ids = [ev.evidence_id]
        bucket[key] = f

    for tb in parsed.tables:
        if not tb.rows or len(tb.rows) < 2:
            continue
        header = tb.rows[0]
        price_ci = (_find_col(header, ("最高限价",))
                    or _find_col(header, ("分项限价",)))
        start_ci = _find_col(header, ("开始时间", "计划开工日期", "计划开始"))
        end_ci = _find_col(header, ("完成时间", "计划竣工日期", "计划完成"))
        lot_ci = _find_col(header, ("标包名称", "包名称", "标包划分", "分标名称"))
        if lot_ci is None:
            continue
        is_price = price_ci is not None
        is_dur = start_ci is not None and end_ci is not None
        if not (is_price or is_dur):
            continue
        unit_wan = is_price and "万" in header[price_ci]

        for ri, row in enumerate(tb.rows[1:], 1):
            cells = [c.strip() for c in row]
            if lot_ci >= len(cells) or not cells[lot_ci]:
                continue
            # 包件名：正则命中且长度足够（结算审核包1）用命中段；
            # 否则用整格（"榆溪换流站土建施工A包"——完整标包名更有辨识度）
            lot = _LOT_NAME_RE.search(cells[lot_ci])
            if not lot:
                m2 = re.search(r"[A-Z一二三四五六七八九十]{1,2}包", cells[lot_ci])
                lot = m2
            lot_name = (lot.group(0) if lot and len(lot.group(0)) >= 4
                        else cells[lot_ci][:40])
            ev = store.add(make_evidence(
                kind="table_cell", source="table",
                page_no=(tb.page_nos[0] if tb.page_nos else 0),
                table_id=tb.table_id, row=ri,
                snippet=" | ".join(cells)[:200]))

            if is_price and price_ci < len(cells):
                raw = cells[price_ci]
                if raw and re.match(r"^\d", raw):
                    cand_val = raw + ("万元" if unit_wan and "万" not in raw else "")
                    from bidmaster.extraction.field_types import _clean_amount
                    if _clean_amount(cand_val, "price_limit") is not None:
                        from bidmaster.extraction.normalize import normalize_amount
                        amt = normalize_amount(cand_val)
                        if amt:
                            y = str(int(amt[0])) if float(amt[0]).is_integer() else str(amt[0])
                            _put(lot_name, "price_limit", cand_val, y, 0.95, ev)

            if is_dur and max(start_ci, end_ci) < len(cells):
                sm, em = _DATE.search(cells[start_ci]), _DATE.search(cells[end_ci])
                if sm and em:
                    dur = (f"{sm.group(1)}年{int(sm.group(2))}月"
                           f"至{em.group(1)}年{int(em.group(2))}月")
                    _put(lot_name, "duration", dur, dur, 0.9, ev)

    return lot_fields


def file_block_ranges(parsed: ParsedDocument) -> list[tuple[int, int, str]]:
    """合并文档中各源文件的块范围 [(start, end, file_name)]（end 不含）。"""
    marks = []
    for i, b in enumerate(parsed.blocks):
        if b.type == "heading" and b.text.startswith("[文件") and "]:" in b.text or \
           (b.type == "heading" and re.match(r"^\[文件\d+:", b.text)):
            marks.append((i, b.text))
    if not marks:
        return [(0, len(parsed.blocks), parsed.file_name)]
    out = []
    for j, (start, label) in enumerate(marks):
        end = marks[j + 1][0] if j + 1 < len(marks) else len(parsed.blocks)
        name = label.split(":", 1)[1].rstrip("]").strip() if ":" in label else label
        out.append((start, end, name))
    return out
