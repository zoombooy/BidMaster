"""OCR 深度链路（扫描件）：在线 MinerU API 为主引擎，可插拔扩展其他 OCR 后端。

在线流程（mineru.net/api/v4）：
  申请上传链接(POST /file-urls/batch) → PUT 上传原件 → 轮询解析状态
  → 下载结果 zip → 解析 content_list.json（type/text/page_idx/bbox）→ ParsedDocument

未配置 Key 或调用失败：扫描页一律记 FAILED_REVIEW（账本保证不静默遗漏），
不阻塞其余文件。所有凭证来自环境变量，严禁写入仓库。
"""
from __future__ import annotations

import io
import json
import re
import time
import zipfile
from pathlib import Path

from bidmaster.config import get_settings
from bidmaster.ingestion.ledger import ProcessingLedger
from bidmaster.schemas.document import Block, BlockStyle, Page, ParsedDocument, TableModel

_TYPE_MAP = {"text": "paragraph", "title": "heading", "table": "table",
             "image": "figure", "equation": "paragraph"}


def parse_pdf_ocr(path: str, doc_id: str, ledger: ProcessingLedger | None = None,
                  empty_pages: list[int] | None = None) -> ParsedDocument:
    backend = get_settings().ocr_backend
    if backend in ("mineru", "mineru-online"):
        return _parse_with_mineru_online(path, doc_id, ledger)
    if backend == "mineru-local":
        return _parse_with_mineru_local_cli(path, doc_id, ledger)
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
                 "warnings": [f"扫描件共 {n} 页未解析：请配置 OCR 后端（mineru/paddle）"]},
    )


# ---------------- 在线 MinerU ----------------
def _parse_with_mineru_online(path: str, doc_id: str,
                              ledger: ProcessingLedger | None) -> ParsedDocument:
    import httpx

    s = get_settings()
    if not s.mineru_api_key:
        return _no_backend(path, doc_id, ledger, "BIDMASTER_MINERU_API_KEY 未配置")
    base = s.mineru_endpoint.rstrip("/")
    headers = {"Authorization": f"Bearer {s.mineru_api_key}"}
    file_bytes = Path(path).read_bytes()
    name = Path(path).name

    # 1) 申请上传链接
    apply_body = {
        "enable_formula": True, "language": "ch", "enable_table": True,
        "files": [{"name": name, "is_ocr": True, "data_id": doc_id}],
    }
    r = httpx.post(f"{base}/file-urls/batch", json=apply_body,
                   headers=headers, timeout=60)
    r.raise_for_status()
    apply_data = r.json().get("data") or {}
    batch_id = apply_data.get("batch_id")
    upload_url = (apply_data.get("file_urls") or [None])[0]
    if not batch_id or not upload_url:
        raise RuntimeError(f"MinerU 申请上传链接失败: {r.text[:200]}")

    # 2) 上传原件（预签名 URL，不带鉴权头）
    put = httpx.put(upload_url, content=file_bytes, timeout=600)
    put.raise_for_status()

    # 3) 轮询解析状态（GET /extract-results/batch/{batch_id}）
    deadline = time.time() + s.mineru_poll_timeout
    zip_url = None
    while time.time() < deadline:
        pr = httpx.get(f"{base}/extract-results/batch/{batch_id}",
                       headers=headers, timeout=60)
        pr.raise_for_status()
        entries = (pr.json().get("data") or {}).get("extract_result") or []
        entry = next((e for e in entries
                      if e.get("file_name") == name or len(entries) == 1), None)
        if entry:
            state = entry.get("state")
            if state == "done":
                zip_url = entry.get("full_zip_url")
                break
            if state == "failed":
                raise RuntimeError(f"MinerU 解析失败: {entry.get('err_msg')}")
        time.sleep(s.mineru_poll_interval)
    if not zip_url:
        raise TimeoutError(f"MinerU 解析超时（>{s.mineru_poll_timeout:.0f}s），batch={batch_id}")

    # 4) 下载结果 zip → content_list.json
    zr = httpx.get(zip_url, timeout=300)
    zr.raise_for_status()
    content_list = None
    with zipfile.ZipFile(io.BytesIO(zr.content)) as z:
        for zf in z.namelist():
            if zf.endswith("content_list.json"):
                content_list = json.loads(z.read(zf).decode("utf-8"))
                break
    if content_list is None:
        raise RuntimeError("MinerU 结果包中无 content_list.json")

    parsed = _build_parsed_from_content_list(content_list, doc_id,
                                             Path(path).name, "pdf_scan")
    parsed.quality.ocr_used = True
    if ledger is not None:
        for pno in sorted({b.page_no for b in parsed.blocks if b.page_no}):
            ledger.register(f"page:{pno}", "page")
            ledger.done(f"page:{pno}")
    return parsed


def _html_table_to_rows(html: str) -> list[list[str]]:
    """MinerU table_body HTML → 行列文本（无 BS4 依赖，正则提取）。"""
    rows = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html or "", re.S):
        cells = [re.sub(r"<[^>]+>", "", c).strip().replace("\n", " ")
                 for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S)]
        if cells:
            rows.append(cells)
    return rows


def _build_parsed_from_content_list(content: list, doc_id: str,
                                    file_name: str, file_type: str) -> ParsedDocument:
    """MinerU content_list（type/text/page_idx/bbox）→ 统一 ParsedDocument。

    table 条目：table_body(html) 解析为行列 → TableModel（供评分链路消费）；
    文本块照常入 blocks。
    """
    blocks: list[Block] = []
    tables: list[TableModel] = []
    char_cursor = 0
    for i, item in enumerate(content):
        text = (item.get("text") or "").strip()
        itype = item.get("type", "text")
        page_no = int(item.get("page_idx", 0)) + 1

        # 表格条目：table_body(html) 解析为行列 → TableModel + 摘要块
        if itype == "table" and (item.get("table_body") or "").strip():
            t_seq = len(tables) + 1
            table_id = f"t-{t_seq:04d}"
            rows = _html_table_to_rows(item.get("table_body"))
            if rows:
                ncols = max(len(r) for r in rows)
                for r in rows:
                    r += [""] * (ncols - len(r))
                tables.append(TableModel(table_id=table_id, page_nos=[page_no],
                                         rows=rows, header_rows=1))
                summary = "\n".join("\t".join(r) for r in rows[:50])
                start = char_cursor
                char_cursor += len(summary) + 1
                blocks.append(Block(
                    block_id=f"b-{i + 1:05d}", type="table", page_no=page_no,
                    char_start=start, char_end=start + len(summary),
                    text=summary, table_ref=table_id))
            continue

        if not text and itype != "table":
            continue
        btype = _TYPE_MAP.get(itype, "paragraph")
        level = 1 if itype == "title" else 0
        start = char_cursor
        char_cursor += len(text) + 1
        blocks.append(Block(
            block_id=f"b-{i + 1:05d}", type=btype, page_no=page_no,
            bbox=item.get("bbox"), char_start=start, char_end=start + len(text),
            text=text, style=BlockStyle(heading_level=level)))
    return ParsedDocument(
        doc_id=doc_id, file_name=file_name, file_type=file_type,
        blocks=blocks, tables=tables,
        full_text="\n".join(b.text for b in blocks),
        quality={"ocr_used": True},
    )


def _no_backend(path: str, doc_id: str, ledger: ProcessingLedger | None,
                reason: str) -> ParsedDocument:
    import fitz
    doc = fitz.open(path)
    n = doc.page_count
    doc.close()
    if ledger is not None:
        for pno in range(1, n + 1):
            ledger.register(f"page:{pno}", "page")
            ledger.fail(f"page:{pno}", reason)
    return ParsedDocument(
        doc_id=doc_id, file_name=Path(path).name, file_type="pdf_scan",
        quality={"ocr_used": False, "warnings": [f"扫描件未解析：{reason}"]},
    )


# ---------------- 本机 CLI（备选，需 pip install mineru + 模型） ----------------
def _parse_with_mineru_local_cli(path: str, doc_id: str,
                                 ledger: ProcessingLedger | None) -> ParsedDocument:
    import subprocess
    import tempfile
    outdir = Path(tempfile.mkdtemp(prefix="bidmaster_mineru_"))
    subprocess.run(["mineru", "-p", str(path), "-o", str(outdir)],
                   check=True, capture_output=True, timeout=3600)
    cl_files = sorted(outdir.rglob("*content_list.json"))
    if not cl_files:
        raise RuntimeError("MinerU 未产出 content_list.json")
    content = json.loads(cl_files[0].read_text(encoding="utf-8"))
    parsed = _build_parsed_from_content_list(content, doc_id,
                                             Path(path).name, "pdf_scan")
    parsed.quality.ocr_used = True
    if ledger is not None:
        for pno in sorted({b.page_no for b in parsed.blocks if b.page_no}):
            ledger.register(f"page:{pno}", "page")
            ledger.done(f"page:{pno}")
    return parsed


# ---------------- PaddleOCR（占位） ----------------
def _parse_with_paddle(path: str, doc_id: str, ledger) -> ParsedDocument:
    raise NotImplementedError(
        "PaddleOCR 后端待接入：调用 PPStructureV3 pipeline，映射为 Block（含 page_no/bbox）。")
