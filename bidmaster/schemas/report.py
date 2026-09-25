"""一期最终交付物：《招标文件结构化解析报告》。"""
from __future__ import annotations

from typing import Optional
from pydantic import BaseModel, Field

from .document import DocQuality
from .evidence import Evidence
from .fields import FieldConflict, FieldExtraction
from .scoring import RequirementItem, ScoreItem, ScoreSumCheck, StarClause


class SectionNode(BaseModel):
    section_id: str
    no: str = ""            # 归一化编号，如 "3" / "3.2"
    title: str = ""
    level: int = 1
    page_start: int = 0
    page_end: int = 0
    block_start: int = 0
    block_end: int = 0      # [block_start, block_end)
    children: list["SectionNode"] = []


class AnchorZone(BaseModel):
    zone: str               # notice / instructions_front / evaluation_method / scoring / ...
    section_id: str = ""
    title: str = ""
    block_start: int = 0
    block_end: int = 0
    page_start: int = 0
    page_end: int = 0


class LedgerSummary(BaseModel):
    total: int = 0
    done: int = 0
    excluded: int = 0
    failed_review: int = 0
    complete_gate_passed: bool = False


class TenderReport(BaseModel):
    doc_id: str
    file_name: str = ""
    generated_at: str = ""
    fields: dict[str, FieldExtraction] = {}
    conflicts: list[FieldConflict] = []
    scores: list[ScoreItem] = []
    requirements: list[RequirementItem] = []
    star_clauses: list[StarClause] = []
    sum_checks: list[ScoreSumCheck] = []
    issues: list[str] = []               # 机械校验/终检问题清单
    sections: list[SectionNode] = []     # 章节树（浅层输出：顶层+二级）
    anchor_zones: list[AnchorZone] = []
    evidence: list[Evidence] = []
    quality: DocQuality = Field(default_factory=DocQuality)
    ledger: LedgerSummary = Field(default_factory=LedgerSummary)
    stats: dict = {}
