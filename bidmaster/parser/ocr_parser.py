"""OCR 深度链路（扫描件）：可插拔后端适配器。

配置 BIDMASTER_OCR_BACKEND 启用：
  - "mineru"  : 调用 MinerU CLI（pip install mineru），输出 Markdown+content_list.json，
                自带页码与块级 bbox，转换为 ParsedDocument。
  - "paddle"  : 调用 PaddleOCR PP-StructureV3（全 Apache-2.0，SaaS 合规链路）。
未配置时：扫描页一律记 FAILED_REVIEW（账本保证不静默遗漏），不阻塞其余文件。
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from bidmaster.config import get_settings
from bidmaster.ingestion.ledger import ProcessingLedger
from bidmaster.schemas.document import Block, BlockStyle, Page, ParsedDocument, TableModel


def parse_pdf_ocr(path: str, doc_id: str, ledger: ProcessingLedger | None = None,
                  empty_pages: list[int] | None = None) -> ParsedDocument:
    backend = get_settings().ocr_backend
    if backend == "mineru":
        return _parse_with_mineru(path, doc_id, ledger)
    if backend == "paddle":
        return _parse_with_paddle(path, doc_id, ledger)
    # 无 OCR 后端：诚实失败，转人工
    import fitz
    doc = fitz.open(path)
    n = doc.page_count
    doc.close()
    if ledger is not None:
        for pno in range(1, n + 1):
            ledger.register(f"page:{pno}", "page")
            ledger.fail(f"page:{pno}", "扫描件但未配置 OCR 后端（BIDMASTER_OCR_BACKEND）")
    return ParsedDocument(
        doc_id=doc_id, file_name=Path(path).name, file_type="pdf_scan",
        quality={"ocr_used": False,
                 "warnings": [f"扫描件共 {n} 页未解析：请配置 MinerU 或 PaddleOCR 后端"]},
    )


def _parse_with_mineru(path: str, doc_id: str, ledger) -> ParsedDocument:
    """调用 MinerU CLI：`mineru -p <pdf> -o <outdir>`，读取 content_list.json。
    content_list 每项含 {type, text, page_idx, bbox}——正好映射到 Block。"""
    import tempfile
    outdir = Path(tempfile.mkdtemp(prefix="bidmaster_mineru_"))
    subprocess.run(
        ["mineru", "-p", str(path), "-o", str(outdir)],
        check=True, capture_output=True, timeout=3600,
    )
    cl_files = sorted(outdir.rglob("*content_list.json"))
    if not cl_files:
        raise RuntimeError("MinerU 未产出 content_list.json")
    content = json.loads(cl_files[0].read_text(encoding="utf-8"))

    blocks: list[Block] = []
    char_cursor = 0
    for i, item in enumerate(content):
        text = (item.get("text") or "").strip()
        if not text:
            continue
        itype = item.get("type", "text")
        btype = {"text": "paragraph", "title": "heading", "table": "table",
                 "image": "figure"}.get(itype, "paragraph")
        page_no = int(item.get("page_idx", 0)) + 1
        btype_lvl = 1 if itype == "title" else 0
        start = char_cursor
        char_cursor += len(text) + 1
        blocks.append(Block(
            block_id=f"b-{i + 1:05d}", type=btype, page_no=page_no,
            bbox=item.get("bbox"), char_start=start, char_end=start + len(text),
            text=text, style=BlockStyle(heading_level=btype_lvl),
        ))
        if ledger is not None:
            ledger.register(f"page:{page_no}", "page")
            ledger.done(f"page:{page_no}")
    full_text = "\n".join(b.text for b in blocks)
    return ParsedDocument(
        doc_id=doc_id, file_name=Path(path).name, file_type="pdf_scan",
        blocks=blocks, full_text=full_text,
        quality={"ocr_used": True},
    )


def _parse_with_paddle(path: str, doc_id: str, ledger) -> ParsedDocument:
    """PaddleOCR PP-StructureV3 适配（占位：按 paddleocr 文档补 pipeline 调用）。
    接口约定与 MinerU 相同：产出 blocks（text/page/bbox）。"""
    raise NotImplementedError(
        "PaddleOCR 后端待接入：在 _parse_with_paddle 中调用 PPStructureV3 pipeline，"
        "将结果映射为 Block（含 page_no/bbox）后复用 mineru 的组装逻辑。")
