"""FastAPI 服务：
  POST /api/analyze                     上传文件 → 跑完整管线 → 返回解析报告
  GET  /api/reports/{doc_id}            获取历史报告
  GET  /api/reports/{doc_id}/evidence/{eid}/page.png   证据原文页渲染（bbox 高亮）
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

app = FastAPI(title="BidMaster 招标文件解析 Agent", version="0.1.0")
pipeline = Pipeline(work_root=Path("work"))
app.include_router(a2a_router)
from bidmaster.mcp_server import router as mcp_router
app.include_router(mcp_router)
ALLOWED_EXT = {".docx", ".doc", ".pdf"}


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


@app.get("/api/reports/{doc_id}")
def get_report(doc_id: str):
    report = pipeline.load_report(doc_id)
    if report is None:
        raise HTTPException(404, f"报告不存在: {doc_id}")
    return report.model_dump(mode="json")


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
    pix = page.get_pixmap(dpi=120)
    png_bytes = pix.tobytes("png")
    doc.close()
    # bbox 高亮在服务端以 SVG 叠加太重——返回页图 + bbox 坐标由前端画框
    return Response(
        content=png_bytes, media_type="image/png",
        headers={"X-Source-File": ev.source_file,
                 "X-Bbox": str(ev.bbox or [])})
