"""DOCX 页码锚定：将 docx 转 PDF（LibreOffice headless），再按文本搜索把页码回写到块。

解决"docx 链路无页码"的证据定位缺口：转换后的 PDF 与源 docx 排版一致，
按块文本前缀在 PDF 页面中搜索即可确定页码，证据链由此获得页码定位能力。

依赖：本机安装 LibreOffice（soffice）或配置 BIDMASTER_SOFFICE 环境变量。
不可用时静默返回 None（调用方降级为块级定位，不阻塞）。
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import fitz


def _find_soffice() -> str | None:
    custom = os.getenv("BIDMASTER_SOFFICE")
    if custom and Path(custom).exists():
        return custom
    for cand in (
        r"C:\Program Files\LibreOffice\program\soffice.exe",
        r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
        "/usr/bin/soffice", "/usr/local/bin/soffice",
    ):
        if Path(cand).exists():
            return cand
    import shutil
    found = shutil.which("soffice")
    return found


def docx_to_pdf(docx_path: str | Path, out_dir: str | Path) -> Path | None:
    """LibreOffice headless 转换 docx → pdf。失败返回 None。"""
    soffice = _find_soffice()
    if not soffice:
        return None
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            [soffice, "--headless", "--convert-to", "pdf",
             "--outdir", str(out_dir), str(docx_path)],
            check=True, capture_output=True, timeout=300)
    except (subprocess.SubprocessError, OSError):
        return None
    pdf = out_dir / (Path(docx_path).stem + ".pdf")
    return pdf if pdf.exists() else None


def build_page_index(pdf_path: str | Path) -> list[dict]:
    """对 PDF 每页建正则化文本索引（供块文本搜索页码）。"""
    doc = fitz.open(str(pdf_path))
    pages = []
    for page in doc:
        text = page.get_text("text")
        pages.append({
            "page_no": page.number + 1,
            "text_norm": _norm_text(text),
            "raw_len": len(text),
        })
    doc.close()
    return pages


def _norm_text(t: str) -> str:
    return re.sub(r"[\s\u3000]+", "", t)


def anchor_blocks(blocks: list, pdf_path: str | Path,
                  max_probe: int = 24) -> int:
    """按块文本前缀在 PDF 页面中搜索页码，回写 block.page_no。返回成功数。"""
    doc = fitz.open(str(pdf_path))
    page_texts = [_norm_text(p.get_text("text")) for p in doc]
    doc.close()
    if not any(page_texts):
        return 0
    anchored = 0
    last_page = 1
    for b in blocks:
        probe = _norm_text((b.text or ""))[:max_probe]
        if len(probe) < 6:
            b.page_no = last_page
            continue
        found = 0
        for pi, pt in enumerate(page_texts, 1):
            if probe in pt:
                found = pi
                break
        if found:
            b.page_no = found
            last_page = found
            anchored += 1
        else:
            b.page_no = last_page  # 找不到 → 沿用上一块页码（顺序文档）
    return anchored
