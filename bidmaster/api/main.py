"""FastAPI 服务：
  POST /api/analyze                     上传文件 → 跑完整管线 → 返回解析报告
  GET  /api/reports                     历史报告列表（供 UI 侧栏）
  GET  /api/reports/{doc_id}            获取历史报告
  GET  /api/reports/{doc_id}/evidence/{eid}/page.png   证据原文页渲染（bbox 高亮）
  GET  /ui                              Web 界面（上传/字段/评分/证据原文对照）
  GET  /healthz
"""
from __future__ import annotations

import io
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import Response

from bidmaster.a2a.routes import router as a2a_router
from bidmaster.config import get_settings
from bidmaster.orchestration.pipeline import Pipeline
from bidmaster.storage.json_store import read_json

app = FastAPI(title="BidMaster 招标文件解析 Agent", version="0.1.0")
pipeline = Pipeline(work_root=Path("work"))
app.include_router(a2a_router)
from bidmaster.mcp_server import router as mcp_router
app.include_router(mcp_router)
ALLOWED_EXT = {".docx", ".doc", ".pdf"}


@app.get("/ui")
def ui():
    """Web 界面（零依赖单页应用，模式参考 MinerU 上传即解析 / RAGFlow 引用高亮）。"""
    from fastapi.responses import FileResponse
    return FileResponse(Path(__file__).parent / "web" / "index.html",
                        media_type="text/html")


@app.get("/healthz")
def healthz():
    s = get_settings()
    return {"status": "ok", "llm": "on" if s.llm_enabled else "off (纯规则模式)",
            "ocr_backend": s.ocr_backend or "未配置"}


MAX_UPLOAD_BYTES = 200 * 1024 * 1024  # 200MB


@app.post("/api/analyze")
async def analyze(file: UploadFile = File(...), force: bool = Form(False)):
    """force=true → 忽略缓存强制重跑（同一文件重复解析时默认走缓存秒回）。"""
    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(400, f"不支持的文件类型 {ext}；支持 {sorted(ALLOWED_EXT)}")
    tmp = Path("work/_uploads")
    tmp.mkdir(parents=True, exist_ok=True)
    # 安全：服务端生成文件名杜绝路径穿越；分块写入限大小；魔数校验防伪装扩展名
    save_path = tmp / f"{__import__('uuid').uuid4().hex}{ext}"
    size = 0
    with save_path.open("wb") as out:
        while chunk := await file.read(1 << 20):
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                out.close()
                save_path.unlink(missing_ok=True)
                raise HTTPException(413, f"文件超过大小上限 {MAX_UPLOAD_BYTES // (1 << 20)}MB")
            out.write(chunk)
    if ext == ".docx" and save_path.read_bytes()[:4] == b"\xd0\xcf\x11\xe0":
        save_path.unlink(missing_ok=True)
        raise HTTPException(400, "文件内容是老式 .doc（仅改了扩展名），请用 Word 另存为 .docx 后重新上传")
    try:
        report = pipeline.run(str(save_path), force=force)
    except NotImplementedError as e:
        raise HTTPException(422, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return report.model_dump(mode="json")


@app.get("/api/reports")
def list_reports():
    """历史报告列表（UI 侧栏用）：扫 work/*/06_report.json 取摘要。"""
    out = []
    for rep_file in sorted(pipeline.work_root.glob("*/06_report.json"),
                           key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            r = pipeline.load_report(rep_file.parent.name)
        except Exception:  # noqa: BLE001 损坏报告跳过
            continue
        if r is None:
            continue
        out.append({
            "doc_id": r.doc_id,
            "file_name": r.file_name,
            "generated_at": r.generated_at,
            "stats": r.stats,
            "issues": len(r.issues),
        })
        if len(out) >= 100:
            break
    return out


@app.get("/api/reports/{doc_id}")
def get_report(doc_id: str):
    report = pipeline.load_report(doc_id)
    if report is None:
        raise HTTPException(404, f"报告不存在: {doc_id}")
    return report.model_dump(mode="json")


@app.get("/api/reports/{doc_id}/evidence/{evidence_id}/context")
def evidence_context(doc_id: str, evidence_id: str):
    """docx 证据的原文上下文（无页码时的"查看原文"）。

    从已解析的源文档结构还原证据周边原文，三类证据三种还原：
    - table_cell → 整张原表（含全部行列），标记命中单元格
    - block      → 命中块 + 前后各 3 块原文
    - llm_quote  → full_text 引用区间 ±120 字，引用段加标记
    """
    report = pipeline.load_report(doc_id)
    if report is None:
        raise HTTPException(404, f"报告不存在: {doc_id}")
    ev = next((e for e in report.evidence if e.evidence_id == evidence_id), None)
    if ev is None:
        raise HTTPException(404, "证据不存在")
    parsed_file = Path("work", doc_id, "02_parsed.json")
    if not parsed_file.exists():
        raise HTTPException(404, "解析产物不存在")
    from bidmaster.schemas.document import ParsedDocument
    parsed = ParsedDocument.model_validate(read_json(parsed_file))

    if ev.kind == "table_cell" and ev.table_id:
        tb = next((t for t in parsed.tables if t.table_id == ev.table_id), None)
        if tb is None:
            raise HTTPException(404, "原表不存在（可能被跨页合并重排）")
        hit = None
        if ev.row is not None and 0 <= ev.row < len(tb.rows):
            col = ev.col if (ev.col is not None and 0 <= ev.col
                             < len(tb.rows[ev.row])) else None
            hit = {"row": ev.row, "col": col}
        # 值单元格定位：片段为"label → value"形态时，在表内搜值文本
        # （跨行 label:value 的证据记的是标签位，值在相邻行，一并标出让原文一眼可读）
        hit_value = None
        if hit and " → " in (ev.snippet or ""):
            want = (ev.snippet.split(" → ", 1)[1] or "").strip()
            if want:
                order = (list(range(hit["row"], len(tb.rows)))
                         + list(range(0, hit["row"])))
                for ri in order:
                    if want in tb.rows[ri]:
                        hit_value = {"row": ri,
                                     "col": tb.rows[ri].index(want)}
                        break
        return {"kind": "table", "table_id": tb.table_id,
                "caption": tb.caption, "rows": tb.rows, "hit": hit,
                "hit_value": hit_value}

    if ev.kind == "llm_quote" and ev.char_end > ev.char_start:
        lo, hi = max(0, ev.char_start - 120), min(len(parsed.full_text),
                                                  ev.char_end + 120)
        return {"kind": "quote",
                "text": parsed.full_text[lo:hi],
                "quote_start": ev.char_start - lo,
                "quote_end": ev.char_end - lo}

    # block 或其他：按 block_id 取上下文；无 block_id 时退回片段本身
    ctx_blocks = []
    if ev.block_id:
        idx = next((i for i, b in enumerate(parsed.blocks)
                    if b.block_id == ev.block_id), None)
        if idx is not None:
            for j in range(max(0, idx - 3), min(len(parsed.blocks), idx + 4)):
                b = parsed.blocks[j]
                text = b.text or (f"［表格 {b.table_ref}］" if b.table_ref else "")
                if text.strip():
                    ctx_blocks.append({"text": text, "type": b.type,
                                       "highlight": j == idx})
    if not ctx_blocks and ev.snippet:
        ctx_blocks.append({"text": ev.snippet, "type": "paragraph",
                           "highlight": True})
    return {"kind": "blocks", "blocks": ctx_blocks}


@app.get("/api/reports/{doc_id}/evidence/{evidence_id}/page.png")
def evidence_page_png(doc_id: str, evidence_id: str):
    """渲染证据所在源文件的对应页——前端"点击字段跳原文"的后端。

    多文件解析时按 evidence.source_file 定位到正确的源文件（不再拿第一份糊弄）。
    """
    report = pipeline.load_report(doc_id)
    if report is None:
        raise HTTPException(404, f"报告不存在: {doc_id}")
    ev = next((e for e in report.evidence if e.evidence_id == evidence_id), None)
    if ev is None or ev.page_no <= 0:
        raise HTTPException(404, "证据不存在或无页码（docx 链路无页码，请转 PDF）")
    # 按证据的 source_file 找到对应源文件副本
    intake_path = Path("work", doc_id, "01_intake.json")
    src = None
    if intake_path.exists() and ev.source_file:
        import json
        meta = json.loads(intake_path.read_text(encoding="utf-8"))
        for i, finfo in enumerate(meta.get("files", [])):
            if finfo.get("file_name") == ev.source_file:
                src = next(Path("work", doc_id).glob(f"source_{i}.*"), None)
                break
    if src is None:
        src = next(Path("work", doc_id).glob("source_*.*"), None) or \
            next(Path("work", doc_id).glob("source.*"), None)
    if src is None:
        raise HTTPException(404, "源文件不存在")
    import fitz
    doc = fitz.open(src)
    if ev.page_no > doc.page_count:
        raise HTTPException(404, "页码越界")
    page = doc[ev.page_no - 1]
    rect = page.rect  # 关文档前取页面尺寸（关闭后页面对象失效）
    pix = page.get_pixmap(dpi=120)
    png_bytes = pix.tobytes("png")
    doc.close()
    # bbox 高亮在服务端以 SVG 叠加太重——返回页图 + bbox 坐标由前端画框；
    # X-Page-Rect 提供页面点尺寸，前端把 bbox 换算成百分比定位（与渲染 DPI 无关）
    return Response(
        content=png_bytes, media_type="image/png",
        headers={"X-Source-File": ev.source_file,
                 "X-Page-Rect": f"{rect.width:.2f},{rect.height:.2f}",
                 "X-Bbox": str(ev.bbox or [])})
