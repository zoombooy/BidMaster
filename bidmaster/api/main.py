"""FastAPI 服务：
  POST /api/analyze                     上传文件 → 跑完整管线 → 返回解析报告
  GET  /api/reports/{doc_id}            获取历史报告
  GET  /api/reports/{doc_id}/evidence/{eid}/page.png   证据原文页渲染（bbox 高亮）
  GET  /healthz
"""
from __future__ import annotations

import io
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
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


@app.post("/api/analyze")
async def analyze(file: UploadFile = File(...)):
    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(400, f"不支持的文件类型 {ext}；支持 {sorted(ALLOWED_EXT)}")
    tmp = Path("work/_uploads")
    tmp.mkdir(parents=True, exist_ok=True)
    save_path = tmp / (file.filename or "upload.docx")
    save_path.write_bytes(await file.read())
    try:
        report = pipeline.run(str(save_path))
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
    """渲染证据所在页并画 bbox 高亮框——前端"点击字段跳原文"的后端。"""
    report = pipeline.load_report(doc_id)
    if report is None:
        raise HTTPException(404, f"报告不存在: {doc_id}")
    ev = next((e for e in report.evidence if e.evidence_id == evidence_id), None)
    if ev is None or ev.page_no <= 0:
        raise HTTPException(404, "证据不存在或无页码（docx 链路无页码，请转 PDF）")
    src = next(Path("work", doc_id).glob("source.*"), None)
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
        headers={"X-Bbox": io.StringIO().write("") or str(ev.bbox or [])})
