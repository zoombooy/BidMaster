"""证据溯源模型：每条抽取结果必须回指原文（无证据不采纳）。"""
from __future__ import annotations

from typing import Optional
from pydantic import BaseModel, Field


class Evidence(BaseModel):
    evidence_id: str
    doc_id: str = ""
    kind: str = "block"  # block | table_cell | llm_quote
    page_no: int = 0
    bbox: Optional[list[float]] = None
    char_start: int = 0
    char_end: int = 0
    block_id: Optional[str] = None
    table_id: Optional[str] = None
    row: Optional[int] = None
    col: Optional[int] = None
    snippet: str = ""  # 原文片段（截断展示用）
    source: str = "rules"  # rules | table | llm


class EvidenceStore(BaseModel):
    """证据注册表：统一分配 id，输出报告时一并序列化。"""
    doc_id: str = ""
    seq: int = 0
    items: list[Evidence] = []

    def add(self, ev: Evidence) -> Evidence:
        self.seq += 1
        ev.evidence_id = f"e-{self.doc_id}-{self.seq:05d}"
        ev.doc_id = self.doc_id
        self.items.append(ev)
        return ev

    def by_id(self, evidence_id: str) -> Optional[Evidence]:
        for it in self.items:
            if it.evidence_id == evidence_id:
                return it
        return None


def make_evidence(
    *,
    doc_id: str = "",
    kind: str = "block",
    source: str = "rules",
    page_no: int = 0,
    bbox: Optional[list[float]] = None,
    char_start: int = 0,
    char_end: int = 0,
    block_id: Optional[str] = None,
    table_id: Optional[str] = None,
    row: Optional[int] = None,
    col: Optional[int] = None,
    snippet: str = "",
) -> Evidence:
    """快捷构造（id 由 EvidenceStore.add 统一分配）。"""
    return Evidence(
        evidence_id="", doc_id=doc_id, kind=kind, source=source, page_no=page_no,
        bbox=bbox, char_start=char_start, char_end=char_end, block_id=block_id,
        table_id=table_id, row=row, col=col, snippet=snippet[:200],
    )
