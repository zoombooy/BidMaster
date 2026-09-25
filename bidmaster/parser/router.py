"""解析路由：按接入层探测结果分发到对应链路，并做跨页表格合并。"""
from __future__ import annotations

from bidmaster.ingestion.ledger import ProcessingLedger
from bidmaster.parser.tables import merge_cross_page_tables
from bidmaster.schemas.document import ParsedDocument


def route_and_parse(path: str, doc_id: str, routed_type: str,
                    ledger: ProcessingLedger,
                    empty_pages: list[int] | None = None) -> ParsedDocument:
    if routed_type == "docx":
        parsed = _lazy("docx_parser").parse_docx(path, doc_id, ledger)
    elif routed_type == "pdf_text":
        parsed = _lazy("pdf_text_parser").parse_pdf_text(
            path, doc_id, ledger, empty_pages=empty_pages)
    elif routed_type == "pdf_scan":
        parsed = _lazy("ocr_parser").parse_pdf_ocr(
            path, doc_id, ledger, empty_pages=empty_pages)
    elif routed_type == "xlsx":
        parsed = _lazy("xlsx_parser").parse_xlsx(path, doc_id, ledger)
    elif routed_type == "doc":
        raise NotImplementedError(
            "老式 .doc 请先转换：soffice --headless --convert-to docx <file>（或 win32com）")
    else:
        raise ValueError(f"不支持的文件类型: {routed_type}")

    parsed.tables = merge_cross_page_tables(parsed.tables)
    return parsed


def _lazy(module: str):
    import importlib
    return importlib.import_module(f"bidmaster.parser.{module}")
