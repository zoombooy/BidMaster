"""废标/否决风险清单（借鉴易标 discardedBids 四象限纪律）。

规则层：解析"否决情形/废标条款"类表格（前附表之二形态：序号|情形事项|具体规定）
        + 归并 ★实质性条款 —— 全部严格溯源。
LLM 层（refine_with_llm，需 Key）："此类标书还可能涉及的"经验补充象限，
        每象限最多 5 条，quote 回查不通过即丢弃。
"""
from __future__ import annotations

import re

from bidmaster.schemas.document import ParsedDocument
from bidmaster.schemas.evidence import EvidenceStore, make_evidence
from bidmaster.schemas.scoring import RejectionItem

# 表头/标题关键词 → 判定为废标条款表
_REJECTION_TABLE_KW = ("否决", "废标", "无效投标", "拒绝投标")
# 情形分类词典（国网前附表之二实测 + 通用）
_CATEGORY_KW = ("主体不符", "资格不符", "资信不良", "实质不符", "程序不符",
                "形式不符", "报价不符", "其他情形")
_KIND_KW = [(r"废标| Failed |否决发包|否决投标", "rejection"),
            (r"无效|不受理|拒绝", "invalid")]


def _classify(text: str) -> tuple[str, str]:
    """→ (kind, category)。"""
    for pat, kind in _KIND_KW:
        if re.search(pat, text):
            return kind, ""
    return "rejection", ""


def extract_rejections(parsed: ParsedDocument, store: EvidenceStore,
                       star_clauses_count: int = 0) -> list[RejectionItem]:
    items: list[RejectionItem] = []
    seq = 0

    for tb in parsed.tables:
        if not tb.rows:
            continue
        header = "".join(tb.rows[0])
        # 表头特征：含 情形/否决/废标/无效 且有 3 列左右（序号|情形事项|具体规定）
        is_rej_table = (any(kw in header for kw in _REJECTION_TABLE_KW)
                        and any(kw in header for kw in ("情形", "事项", "原因", "具体")))
        if not is_rej_table:
            # 也识别"具体规定/认定情形"类表头
            is_rej_table = ("具体规定" in header or "认定情形" in header) and len(tb.rows) >= 3
        if not is_rej_table:
            continue

        for ri, row in enumerate(tb.rows[1:], 1):
            cells = [c.strip() for c in row]
            joined = " ".join(c for c in cells if c)
            if not joined or len(joined) < 8:
                continue
            category = next((kw for kw in _CATEGORY_KW if any(kw in c for c in cells)), "")
            # 正文取最长单元格；分类取含分类词的短单元格
            text_cell = max(cells, key=len)
            kind, _ = _classify(joined)
            ev = store.add(make_evidence(
                kind="table_cell", source="table",
                page_no=(tb.page_nos[0] if tb.page_nos else 0),
                table_id=tb.table_id, row=ri, snippet=joined[:200]))
            seq += 1
            items.append(RejectionItem(
                rej_id=f"RJ-{seq:03d}", kind=kind, origin="explicit",
                category=category or "其他情形", text=text_cell[:400],
                severity="high", source="rejection_table",
                evidence_ids=[ev.evidence_id], confidence=0.9))

    # ★实质性条款并入（不重复计入 StarClause）
    for b in parsed.blocks:
        if "★" in (b.text or "") and "★号条款" not in b.text and "带★" not in b.text:
            ev = store.add(make_evidence(
                kind="block", source="rules", page_no=b.page_no, bbox=b.bbox,
                block_id=b.block_id, snippet=b.text[:200]))
            seq += 1
            items.append(RejectionItem(
                rej_id=f"RJ-{seq:03d}", kind="rejection", origin="explicit",
                category="实质性条款（★）", text=b.text.strip()[:400],
                severity="high", source="star_clause",
                evidence_ids=[ev.evidence_id], confidence=0.9))
    return items
