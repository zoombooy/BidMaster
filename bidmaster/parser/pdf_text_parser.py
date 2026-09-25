"""PDF 文本层快速链路：PyMuPDF 抽取段落块（含 bbox/字号/加粗）+ find_tables 提表格。

标题启发式：字号显著大于正文字号（>1.12x）且长度较短 → 标题候选；
层级由不同字号档位排序决定，结构层再用编号正则精修。
"""
from __future__ import annotations

from collections import Counter

from bidmaster.ingestion.ledger import ProcessingLedger
from bidmaster.schemas.document import Block, BlockStyle, Page, ParsedDocument, TableModel


def _page_blocks(page) -> list[dict]:
    """返回 [{text, bbox, size, bold}]，同页内按阅读序。"""
    data = page.get_text("dict")
    out = []
    for blk in data.get("blocks", []):
        if blk.get("type") != 0:
            continue
        lines = blk.get("lines", [])
        spans = [sp for ln in lines for sp in ln.get("spans", [])]
        text = "".join(sp.get("text", "") for sp in spans).strip()
        if not text:
            continue
        main_size = max(spans, key=lambda s: len(s.get("text", ""))).get("size", 0.0)
        bold = bool(spans[0].get("flags", 0) & 16) if spans else False
        out.append({"text": text, "bbox": list(blk["bbox"]),
                    "size": round(main_size, 1), "bold": bold})
    return out


def _body_size(all_sizes: Counter) -> float:
    if not all_sizes:
        return 0.0
    return all_sizes.most_common(1)[0][0]


def parse_pdf_text(path: str, doc_id: str, ledger: ProcessingLedger | None = None,
                   empty_pages: list[int] | None = None) -> ParsedDocument:
    import fitz

    doc = fitz.open(path)
    blocks: list[Block] = []
    tables: list[TableModel] = []
    pages: list[Page] = []
    size_counter: Counter = Counter()
    page_raw: list[list[dict]] = []

    empty_pages = set(empty_pages or [])

    for page in doc:
        pno = page.number + 1
        pages.append(Page(page_no=pno, width=page.rect.width, height=page.rect.height))
        if pno in empty_pages:
            if ledger is not None:
                ledger.exclude(f"page:{pno}", "空白页/无文本")
            page_raw.append([])
            continue
        pbs = _page_blocks(page)
        page_raw.append(pbs)
        for b in pbs:
            size_counter[b["size"]] += len(b["text"])

    body = _body_size(size_counter)
    # 标题字号档位：大于正文 1.12 倍的字号降序排列 → level 1..N
    heading_sizes = sorted({s for s in size_counter if s > body * 1.12}, reverse=True)
    size_rank = {s: i + 1 for i, s in enumerate(heading_sizes)}

    char_cursor = 0
    b_seq = 0
    t_seq = 0
    for page in doc:
        pno = page.number + 1
        if ledger is not None:
            ledger.register(f"page:{pno}", "page")
        if pno in empty_pages:
            continue

        for b in page_raw[pno - 1]:
            b_seq += 1
            level = 0
            if b["size"] > body * 1.12 and len(b["text"]) <= 60:
                level = size_rank.get(b["size"], len(heading_sizes) or 1)
            start = char_cursor
            char_cursor += len(b["text"]) + 1
            blocks.append(Block(
                block_id=f"b-{b_seq:05d}", type="heading" if level else "paragraph",
                page_no=pno, bbox=b["bbox"], char_start=start,
                char_end=start + len(b["text"]), text=b["text"],
                style=BlockStyle(heading_level=level, font_size=b["size"], bold=b["bold"]),
            ))
        if ledger is not None:
            ledger.done(f"page:{pno}")

        # 表格提取
        try:
            finder = page.find_tables()
            page_tables = finder.tables
        except Exception:
            page_tables = []
        for tb in page_tables:
            t_seq += 1
            table_id = f"t-{t_seq:04d}"
            try:
                rows = [["" if c is None else str(c).strip().replace("\n", " ")
                         for c in row] for row in tb.extract()]
            except Exception:
                continue
            # 保留原始行（纵向合并不做向下填充，避免污染评分表首列）
            tables.append(TableModel(
                table_id=table_id, page_nos=[pno], rows=rows, header_rows=1,
                cell_bboxes=[list(tb.bbox)],
            ))
            b_seq += 1
            summary = "\n".join("\t".join(r) for r in rows[:50])
            start = char_cursor
            char_cursor += len(summary) + 1
            blocks.append(Block(
                block_id=f"b-{b_seq:05d}", type="table", page_no=pno,
                bbox=list(tb.bbox), char_start=start, char_end=start + len(summary),
                text=summary, table_ref=table_id,
            ))

    doc.close()
    full_text = "\n".join(b.text for b in blocks)
    return ParsedDocument(
        doc_id=doc_id, file_name=path.split("\\")[-1].split("/")[-1],
        file_type="pdf_text", pages=pages, blocks=blocks, tables=tables,
        full_text=full_text,
    )
