"""统一解析结果模型：三条解析链路（docx / pdf 文本层 / OCR）输出同构的 ParsedDocument，
后续所有抽取结果的溯源都指向 block_id / table_id，可反查页码与坐标。"""
from __future__ import annotations

from typing import Optional
from pydantic import BaseModel, Field


class Page(BaseModel):
    page_no: int  # 1-based
    width: float = 0.0
    height: float = 0.0


class BlockStyle(BaseModel):
    heading_level: int = 0  # 0=非标题, 1..9 对应 第X章..条款层级
    font_size: float = 0.0
    bold: bool = False


class Block(BaseModel):
    block_id: str
    type: str = "paragraph"  # paragraph | heading | table | figure | header | footer
    page_no: int = 0
    bbox: Optional[list[float]] = None  # [x0, y0, x1, y1]，PDF 页面坐标；docx 为空
    char_start: int = 0  # 在 full_text 中的字符偏移
    char_end: int = 0
    text: str = ""
    style: BlockStyle = Field(default_factory=BlockStyle)
    table_ref: Optional[str] = None  # type=table 时指向 TableModel.table_id


class TableCell(BaseModel):
    row: int
    col: int
    text: str = ""
    page_no: int = 0
    bbox: Optional[list[float]] = None


class TableModel(BaseModel):
    table_id: str
    page_nos: list[int] = []  # 跨页合并后可能包含多个页码
    rows: list[list[str]] = []  # 逻辑展开后的单元格文本（合并单元格已填充）
    header_rows: int = 1
    caption: str = ""
    cell_bboxes: list = []  # 与 rows 对齐的坐标（PDF 可用，docx 为空）


class DocQuality(BaseModel):
    ocr_used: bool = False
    text_layer: bool = True  # PDF 是否有可用文本层
    low_confidence_pages: list[int] = []
    warnings: list[str] = []


class ParsedDocument(BaseModel):
    doc_id: str
    file_name: str
    file_type: str  # docx | doc | pdf_text | pdf_scan
    sha256: str = ""
    pages: list[Page] = []
    blocks: list[Block] = []
    tables: list[TableModel] = []
    full_text: str = ""
    quality: DocQuality = Field(default_factory=DocQuality)

    def blocks_in(self, start: int, end: int) -> list[Block]:
        """按块索引区间取块 [start, end)。"""
        return self.blocks[start:end]
