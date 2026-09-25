"""Word（.docx）原生链路：python-docx 提取段落/标题/表格，docx2python 补充页眉页脚。

标题层级双通道：样式名（Heading N / 标题 N）+ 大纲级别（w:outlineLvl）。
老式 .doc 请先用 LibreOffice headless 转 docx（见 README）。
"""
from __future__ import annotations

import re

from bidmaster.ingestion.ledger import ProcessingLedger
from bidmaster.schemas.document import Block, BlockStyle, Page, ParsedDocument, TableModel

_HEADING_STYLE = re.compile(r"^(?:Heading|标题)\s*(\d)$", re.I)


def _heading_level_from_style(style_name: str | None) -> int:
    if not style_name:
        return 0
    m = _HEADING_STYLE.match(style_name.strip())
    return int(m.group(1)) if m else 0


def _heading_level_from_outline(paragraph) -> int:
    """读取 w:outlineLvl 大纲级别（0-based → 1-based）。"""
    ppr = paragraph._p.pPr  # noqa: SLF001
    if ppr is None:
        return 0
    node = ppr.find(
        "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}outlineLvl")
    if node is None:
        return 0
    try:
        return int(node.get(
            "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}val")) + 1
    except (TypeError, ValueError):
        return 0


def _extract_table(tbl, table_id: str, page_no: int = 0) -> TableModel:
    """提取表格：横向合并单元格（python-docx 返回重复单元格）做连续去重；
    纵向合并不做向下填充（避免污染评分表首列），类别继承交给评分解析层。"""
    rows: list[list[str]] = []
    for row in tbl.rows:
        cells: list[str] = []
        for cell in row.cells:
            text = cell.text.strip().replace("\n", " ")
            if cells and cells[-1] == text:  # 横向合并 → 连续重复去重
                continue
            cells.append(text)
        rows.append(cells)
    ncols = max((len(r) for r in rows), default=0)
    for r in rows:
        r += [""] * (ncols - len(r))
    return TableModel(
        table_id=table_id, page_nos=[page_no], rows=rows,
        header_rows=1 if rows else 0,
    )


def parse_docx(path: str, doc_id: str, ledger: ProcessingLedger | None = None) -> ParsedDocument:
    from docx import Document

    document = Document(path)
    blocks: list[Block] = []
    tables: list[TableModel] = []
    char_cursor = 0
    b_seq = 0
    t_seq = 0

    def add_block(text: str, *, btype: str, page_no: int = 0, style: BlockStyle | None = None,
                  table_ref: str | None = None) -> None:
        nonlocal char_cursor, b_seq
        b_seq += 1
        start = char_cursor
        char_cursor += len(text) + 1  # +1 为块间换行
        blocks.append(Block(
            block_id=f"b-{b_seq:05d}", type=btype, page_no=page_no,
            char_start=start, char_end=start + len(text), text=text,
            style=style or BlockStyle(), table_ref=table_ref,
        ))

    # 按文档流顺序遍历正文元素（段落与表格交错）
    body = document.element.body
    from docx.table import Table as DocxTable
    from docx.text.paragraph import Paragraph as DocxParagraph

    for child in body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            para = DocxParagraph(child, document)
            text = para.text.strip()
            if not text:
                continue
            level = _heading_level_from_style(para.style.name if para.style else None) \
                or _heading_level_from_outline(para)
            btype = "heading" if level else "paragraph"
            add_block(text, btype=btype,
                      style=BlockStyle(heading_level=level))
        elif tag == "tbl":
            t_seq += 1
            table_id = f"t-{t_seq:04d}"
            tbl = DocxTable(child, document)
            model = _extract_table(tbl, table_id)
            tables.append(model)
            summary = "\n".join("\t".join(r) for r in model.rows[:50])
            add_block(summary, btype="table", table_ref=table_id)

    full_text = "\n".join(b.text for b in blocks)
    doc = ParsedDocument(
        doc_id=doc_id, file_name=path.split("\\")[-1].split("/")[-1], file_type="docx",
        pages=[Page(page_no=0)], blocks=blocks, tables=tables, full_text=full_text,
    )
    doc.quality.warnings.append("docx 链路无页码信息（page_no=0）；如需页码/bbox 请转 PDF 后解析")
    if ledger is not None:
        ledger.register("file:main", "file")
        ledger.done("file:main")
    return doc
