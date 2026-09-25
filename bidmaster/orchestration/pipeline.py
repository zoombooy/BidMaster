"""编排管线：确定性状态机 + 每阶段 checkpoint（产物落盘，可恢复/定向重跑/级联失效）。

  intake → parse → structure → fields → scoring → report
                      人工闸门：report 生成后默认 pending，确认后写 reviewed.json

上游重跑（--force）自动级联删除下游产物（borrowed from 易标 force_rerun）。
"""
from __future__ import annotations

import datetime as _dt
import shutil
from pathlib import Path

from bidmaster.chunking.splitter import split_document
from bidmaster.config import get_settings
from bidmaster.extraction.fields import FieldExtractor
from bidmaster.ingestion import intake as intake_mod
from bidmaster.ingestion.ledger import ProcessingLedger
from bidmaster.llm.client import LLMClient
from bidmaster.parser.router import route_and_parse
from bidmaster.schemas.document import ParsedDocument
from bidmaster.schemas.evidence import EvidenceStore
from bidmaster.schemas.fields import STATUS_FOUND
from bidmaster.schemas.report import (AnchorZone, LedgerSummary, SectionNode,
                                      TenderReport)
from bidmaster.scoring.requirements import RequirementBuilder
from bidmaster.scoring.table_scores import extract_score_items
from bidmaster.scoring.validate import required_fields_check, validate_scores
from bidmaster.storage.json_store import read_json, write_json, workspace_for
from bidmaster.structure.anchors import find_anchor_zones
from bidmaster.structure.toc import build_section_tree, flatten

STAGE_FILES = {
    "intake": "01_intake.json",
    "parse": "02_parsed.json",
    "structure": "03_structure.json",
    "fields": "04_fields.json",
    "scoring": "05_scoring.json",
    "report": "06_report.json",
}
_DOWNSTREAM = {  # 级联失效表
    "intake": ["parse", "structure", "fields", "scoring", "report"],
    "parse": ["structure", "fields", "scoring", "report"],
    "structure": ["fields", "scoring", "report"],
    "fields": ["scoring", "report"],
    "scoring": ["report"],
    "report": [],
}


class Pipeline:
    def __init__(self, work_root: Path | str = "work"):
        self.work_root = Path(work_root)
        self.settings = get_settings()

    # ---------- 对外 ----------
    def run(self, file_path: str, *, force: bool = False,
            use_llm: bool = True) -> TenderReport:
        ws, doc_id = self._ensure_workspace(file_path, force)
        meta = self._stage_intake(ws, doc_id, file_path, force)
        parsed = self._stage_parse(ws, meta, force)
        struct = self._stage_structure(ws, parsed, force)
        fields, conflicts = self._stage_fields(ws, parsed, struct, use_llm, force)
        scoring = self._stage_scoring(ws, parsed, struct, fields, force)
        report = self._stage_report(ws, meta, parsed, struct, fields, conflicts,
                                    scoring, force)
        return report

    def load_report(self, doc_id: str) -> TenderReport | None:
        p = self.work_root / doc_id / STAGE_FILES["report"]
        if not p.exists():
            return None
        return TenderReport.model_validate(read_json(p))

    # ---------- 工作区 ----------
    def _ensure_workspace(self, file_path: str, force: bool):
        # doc_id 由内容 SHA + 文件名决定 → 同一文件重跑复用工作区
        info = intake_mod.intake(Path(file_path))
        doc_id = info["sha256"][:12]
        ws = workspace_for(self.work_root, doc_id)
        if force:
            for stage in ("parse", "structure", "fields", "scoring", "report"):
                f = ws / STAGE_FILES[stage]
                if f.exists():
                    f.unlink()
        # 保存源文件副本（供 API 页面渲染）
        src = ws / f"source{Path(file_path).suffix.lower()}"
        if not src.exists():
            shutil.copy(file_path, src)
        return ws, doc_id

    # ---------- 各阶段 ----------
    def _stage_intake(self, ws: Path, doc_id: str, file_path: str, force: bool) -> dict:
        f = ws / STAGE_FILES["intake"]
        if f.exists() and not force:
            return read_json(f)
        info = intake_mod.intake(Path(file_path))
        info["doc_id"] = doc_id
        write_json(f, info)
        return info

    def _stage_parse(self, ws: Path, meta: dict, force: bool) -> ParsedDocument:
        f = ws / STAGE_FILES["parse"]
        if f.exists() and not force:
            return ParsedDocument.model_validate(read_json(f))
        ledger = ProcessingLedger(doc_id=meta["doc_id"])
        probe = meta.get("probe") or {}
        parsed = route_and_parse(meta["path"], meta["doc_id"], meta["routed_type"],
                                 ledger, empty_pages=probe.get("empty_pages"))
        parsed.sha256 = meta.get("sha256", "")
        parsed.quality.warnings.extend(self._quality_warnings(meta))
        write_json(ws / "ledger.json",
                   {**ledger.model_dump(mode="json"), "summary": ledger.summary()})
        write_json(f, parsed.model_dump(mode="json"))
        return parsed

    def _stage_structure(self, ws: Path, parsed: ParsedDocument,
                         force: bool) -> dict:
        f = ws / STAGE_FILES["structure"]
        if f.exists() and not force:
            return read_json(f)
        tops = build_section_tree(parsed)
        zones = find_anchor_zones(parsed)
        chunks = split_document(parsed, tops)
        data = {
            "sections": [s.model_dump(mode="json") for s in flatten(tops, max_level=3)],
            "zones": [z.model_dump(mode="json") for z in zones],
            "chunk_count": len(chunks),
        }
        write_json(f, data)
        return data

    def _stage_fields(self, ws: Path, parsed: ParsedDocument, struct: dict,
                      use_llm: bool, force: bool):
        f = ws / STAGE_FILES["fields"]
        if f.exists() and not force:
            data = read_json(f)
            return (data["fields"], [c for c in data.get("conflicts", [])])
        store = self._load_store(ws)
        zones = [AnchorZone.model_validate(z) for z in struct["zones"]]
        llm = LLMClient() if use_llm else None
        extractor = FieldExtractor(parsed, zones, store, llm)
        fields, conflicts = extractor.extract_all(use_llm=use_llm)
        # 统一序列化边界：阶段产物一律为 dict（磁盘加载路径同样返回 dict）
        fields_d = {k: v.model_dump(mode="json") for k, v in fields.items()}
        conflicts_d = [c.model_dump(mode="json") for c in conflicts]
        write_json(ws / "evidence.json", store.model_dump(mode="json"))
        write_json(f, {"fields": fields_d, "conflicts": conflicts_d})
        return fields_d, conflicts_d

    def _stage_scoring(self, ws: Path, parsed: ParsedDocument, struct: dict,
                       fields: dict, force: bool) -> dict:
        f = ws / STAGE_FILES["scoring"]
        if f.exists() and not force:
            return read_json(f)
        store = self._load_store(ws)
        zones = [AnchorZone.model_validate(z) for z in struct["zones"]]
        scores, declared = extract_score_items(parsed, zones, store)
        builder = RequirementBuilder(parsed, zones, scores, store)
        reqs, stars = builder.build()
        write_json(ws / "evidence.json", store.model_dump(mode="json"))
        data = {
            "scores": [s.model_dump(mode="json") for s in scores],
            "requirements": [r.model_dump(mode="json") for r in reqs],
            "stars": [s.model_dump(mode="json") for s in stars],
            "declared": declared,
        }
        write_json(f, data)
        return data

    def _stage_report(self, ws: Path, meta: dict, parsed: ParsedDocument,
                      struct: dict, fields: dict, conflicts: list,
                      scoring: dict, force: bool) -> TenderReport:
        f = ws / STAGE_FILES["report"]
        if f.exists() and not force:
            return TenderReport.model_validate(read_json(f))

        store = self._load_store(ws)
        issues: list[str] = []
        field_values = {k: str(v.get("value_normalized") or "")
                        for k, v in fields.items() if isinstance(v, dict)}
        sum_checks = validate_scores(
            [s for s in (scoring["scores"])],
            scoring.get("declared", []), field_values, issues)
        required_fields_check(fields, issues)
        # 置信度复核队列
        review = [k for k, v in fields.items()
                  if isinstance(v, dict) and v.get("status") == STATUS_FOUND
                  and float(v.get("confidence") or 0) < self.settings.review_threshold]
        if review:
            issues.append(f"低置信度字段（建议人工复核）: {review}")

        ledger_path = ws / "ledger.json"
        ledger_sum = LedgerSummary()
        if ledger_path.exists():
            s = read_json(ledger_path)
            sm = s.get("summary") or {}
            ledger_sum = LedgerSummary(
                total=sm.get("total", 0), done=sm.get("done", 0),
                excluded=sm.get("excluded", 0), failed_review=sm.get("failed_review", 0),
                complete_gate_passed=sm.get("complete_gate_passed", False))
            if not ledger_sum.complete_gate_passed:
                issues.append("处理账本存在未终态对象（complete gate 未通过）")

        report = TenderReport(
            doc_id=meta["doc_id"], file_name=meta["file_name"],
            generated_at=_dt.datetime.now().isoformat(timespec="seconds"),
            fields={k: v for k, v in fields.items()},
            conflicts=conflicts,
            scores=scoring["scores"],
            requirements=scoring["requirements"],
            star_clauses=scoring["stars"],
            sum_checks=sum_checks,
            issues=issues,
            sections=struct["sections"],
            anchor_zones=struct["zones"],
            evidence=store.items,
            quality=parsed.quality,
            ledger=ledger_sum,
            stats={
                "blocks": len(parsed.blocks), "tables": len(parsed.tables),
                "chunks": struct.get("chunk_count", 0),
                "fields_found": sum(1 for v in fields.values()
                                    if v.get("status") == STATUS_FOUND),
                "fields_total": len(fields),
                "score_items": len(scoring["scores"]),
                "requirements": len(scoring["requirements"]),
                "star_clauses": len(scoring["stars"]),
                "gate": "pending_review",
                "llm": "on" if (use_llm_flag() and self.settings.llm_enabled) else "off",
            },
        )
        write_json(f, report.model_dump(mode="json"))
        return report

    # ---------- 工具 ----------
    def _load_store(self, ws: Path) -> EvidenceStore:
        p = ws / "evidence.json"
        if p.exists():
            return EvidenceStore.model_validate(read_json(p))
        return EvidenceStore()

    @staticmethod
    def _quality_warnings(meta: dict) -> list[str]:
        out: list[str] = []
        if meta["routed_type"] == "pdf_scan":
            out.append("扫描件：当前以 OCR 链路处理（未配置则该文件标记 FAILED_REVIEW）")
        if meta["routed_type"] == "doc":
            out.append("老式 .doc：请先用 LibreOffice 转 docx 获得最佳效果")
        return out


def use_llm_flag() -> bool:  # 默认开启（未配置 LLM 时自动退化为纯规则）
    return True
