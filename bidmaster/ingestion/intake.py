"""接入层：文件清点（SHA-256 去重/类型识别/PDF 文本层探测）。"""
from __future__ import annotations

import hashlib
import uuid
from pathlib import Path

from bidmaster.config import get_settings


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def probe_pdf_text_layer(path: Path) -> tuple[bool, int, list[int]]:
    """探测 PDF 是否有可用文本层。

    返回 (has_text_layer, total_pages, empty_pages)。判定标准：平均每页有效字符数
    低于阈值（默认 30）视为扫描件。空页（页码页/封面）自动排除不计入平均。
    """
    import fitz  # PyMuPDF

    settings = get_settings()
    doc = fitz.open(path)
    total = doc.page_count
    char_counts: list[int] = []
    for page in doc:
        txt = page.get_text("text").strip()
        char_counts.append(len(txt))
    doc.close()
    non_empty = [c for c in char_counts if c > 0]
    avg = (sum(non_empty) / len(non_empty)) if non_empty else 0
    empty_pages = [i + 1 for i, c in enumerate(char_counts) if c == 0]
    return (avg >= settings.pdf_text_min_chars_per_page), total, empty_pages


def detect_type(path: Path) -> tuple[str, dict]:
    """识别文件类型并返回 (routed_type, probe_info)。

    routed_type ∈ docx | doc | pdf_text | pdf_scan | unsupported
    """
    ext = path.suffix.lower()
    probe: dict = {}
    if ext == ".docx":
        return "docx", probe
    if ext == ".doc":
        return "doc", probe
    if ext == ".pdf":
        has_text, pages, empty = probe_pdf_text_layer(path)
        probe = {"pages": pages, "empty_pages": empty, "has_text_layer": has_text}
        return ("pdf_text" if has_text else "pdf_scan"), probe
    if ext in (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"):
        return "pdf_scan", probe  # 图片走 OCR 链路（同扫描件）
    return "unsupported", probe


def new_doc_id() -> str:
    return uuid.uuid4().hex[:12]


def intake(path: Path) -> dict:
    """清点入口：返回 doc_id、路由类型、探针信息与 SHA-256。"""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"文件不存在: {path}")
    routed, probe = detect_type(path)
    return {
        "doc_id": new_doc_id(),
        "file_name": path.name,
        "path": str(path.resolve()),
        "sha256": sha256_of(path),
        "routed_type": routed,
        "probe": probe,
    }
