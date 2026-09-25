"""字段抽取管线（四级）：表格标签 > 锚区规则 > 全文规则 > LLM 兜底。

每级产出候选（带证据/置信度），跨来源冲突全部记录；置信度 = 来源等级 × 证据校验。
LLM 兜底要求模型返回 source_quote，代码回查原文验证——无证据不采纳。
"""
from __future__ import annotations

import bisect

from bidmaster.extraction.llm_fallback import llm_fill_missing
from bidmaster.extraction.rules import (FIELD_RULES, NORM_AMOUNT, NORM_DATETIME,
                                        NORM_DURATION, RulePack, _TABLE_LABELS, rule_hit)
from bidmaster.llm.client import LLMClient
from bidmaster.schemas.document import Block, ParsedDocument
from bidmaster.schemas.evidence import EvidenceStore, make_evidence
from bidmaster.schemas.fields import (FIELD_CATALOG, STATUS_FAILED, STATUS_FOUND,
                                      STATUS_NOT_FOUND, FieldCandidate, FieldConflict,
                                      FieldExtraction, empty_extraction)
from bidmaster.schemas.report import AnchorZone


class FieldExtractor:
    def __init__(self, parsed: ParsedDocument, zones: list[AnchorZone],
                 store: EvidenceStore, llm: LLMClient | None = None):
        self.parsed = parsed
        self.zones = zones
        self.store = store
        self.llm = llm

    # ---------- 主入口 ----------
    def extract_all(self, use_llm: bool = True):
        results: dict[str, FieldExtraction] = {}
        for key in FIELD_RULES:
            results[key] = self.extract_one(key)
        if use_llm and self.llm is not None and self.llm.enabled:
            missing = [k for k, v in results.items()
                       if v.status == STATUS_NOT_FOUND and k in FIELD_RULES]
            if missing:
                llm_fill_missing(self.parsed, self.zones, missing, results,
                                 self.store, self.llm)

        conflicts = self._detect_conflicts(results)
        # 状态收敛
        for f in results.values():
            if f.status == STATUS_NOT_FOUND:
                f.note = "文档中未发现该字段（检索过表格标签、锚区与全文）"
        # LLM 补齐后仍可能缺失 → 不再标 failed（failed 保留给 LLM 有配置但校验失败的情形）
        return results, conflicts

    def extract_one(self, field_key: str) -> FieldExtraction:
        pack = FIELD_RULES[field_key]
        out = empty_extraction(field_key)
        candidates: list[FieldCandidate] = []

        # 1) 表格标签（前附表金矿）
        cand = self._from_tables(pack)
        if cand:
            candidates.extend(cand)
        # 2) 锚区规则
        cand = self._from_zones(pack)
        if cand:
            candidates.extend(cand)
        # 3) 全文兜底
        cand = self._from_fulltext(pack)
        if cand:
            candidates.extend(cand)

        if not candidates:
            return out  # not_found

        out.candidates = candidates
        best = max(candidates, key=lambda c: c.confidence)
        out.value_raw = best.value_raw
        out.value_normalized = best.value_normalized
        out.value_unit = self._unit_of(pack.normalizer, best.value_normalized)
        out.confidence = best.confidence
        out.status = STATUS_FOUND
        out.evidence_ids = [best.evidence_id] if best.evidence_id else []
        return out

    # ---------- 来源 1：表格标签 ----------
    def _from_tables(self, pack: RulePack) -> list[FieldCandidate]:
        label_re = _TABLE_LABELS.get(pack.field_key)
        out: list[FieldCandidate] = []
        if label_re is None:
            return out
        from bidmaster.extraction.rules import _INVALID_VALUES
        for tb in self.parsed.tables:
            for ri, row in enumerate(tb.rows):
                for ci, cell in enumerate(row):
                    if not label_re.match(cell.strip().rstrip("：:")):
                        continue
                    value = self._cell_value(tb.rows, ri, ci)
                    if not value or value in _INVALID_VALUES:
                        continue
                    norm = self._normalize(pack, value)
                    if norm is None:
                        continue
                    norm_val, norm_str = norm
                    ev = self.store.add(make_evidence(
                        kind="table_cell", source="table",
                        page_no=(tb.page_nos[0] if tb.page_nos else 0),
                        table_id=tb.table_id, row=ri, col=ci,
                        snippet=f"{cell.strip()} → {value[:80]}"))
                    out.append(FieldCandidate(
                        value_raw=value, value_normalized=norm_str, source="table",
                        confidence=0.95, evidence_id=ev.evidence_id,
                        zone="instructions_front" if pack.field_key in _TABLE_LABELS else ""))
                    break  # 该行只取一次
        return out

    @staticmethod
    def _cell_value(rows: list[list[str]], ri: int, ci: int) -> str:
        """标签右侧取值；同行右侧为空则取同列下一行。"""
        row = rows[ri]
        for cj in range(ci + 1, len(row)):
            v = row[cj].strip()
            if v and v not in ("—", "/", "无"):
                return v
        if ri + 1 < len(rows) and ci < len(rows[ri + 1]):
            v = rows[ri + 1][ci].strip()
            if v and v not in ("—", "/", "无"):
                return v
        return ""

    # ---------- 来源 2/3：锚区与全文 ----------
    def _from_zones(self, pack: RulePack) -> list[FieldCandidate]:
        out: list[FieldCandidate] = []
        for zone in self.zones:
            if zone.zone not in pack.preferred_zones:
                continue
            blocks = self.parsed.blocks[zone.block_start:zone.block_end]
            out.extend(self._hit_blocks(pack, blocks, zone=zone.zone, confidence=0.90))
        return out

    def _from_fulltext(self, pack: RulePack) -> list[FieldCandidate]:
        return self._hit_blocks(pack, self.parsed.blocks, zone="", confidence=0.78)

    def _hit_blocks(self, pack: RulePack, blocks: list[Block], zone: str,
                    confidence: float) -> list[FieldCandidate]:
        out: list[FieldCandidate] = []
        for b in blocks:
            hit = rule_hit(pack, b.text)
            if not hit:
                continue
            raw, norm_value, _span = hit  # norm_value 恒为标量
            ev = self.store.add(make_evidence(
                kind="block", source="rules", page_no=b.page_no, bbox=b.bbox,
                char_start=b.char_start, char_end=b.char_end,
                block_id=b.block_id, snippet=b.text[:200]))
            out.append(FieldCandidate(
                value_raw=raw, value_normalized=str(norm_value), source="rules",
                confidence=confidence, evidence_id=ev.evidence_id, zone=zone))
        return out

    # ---------- 工具 ----------
    @staticmethod
    def _normalize(pack: RulePack, value: str):
        """返回 (归一化值, 展示字符串) | None。"""
        if pack.normalizer == NORM_AMOUNT:
            from bidmaster.extraction.normalize import normalize_amount
            r = normalize_amount(value)
            return (r[0], _fmt_number(r[0])) if r else None
        if pack.normalizer == NORM_DATETIME:
            from bidmaster.extraction.normalize import normalize_datetime
            r = normalize_datetime(value)
            return (r[0], r[0]) if r else None
        if pack.normalizer == NORM_DURATION:
            from bidmaster.extraction.normalize import normalize_duration
            r = normalize_duration(value)
            return (r[0], f"{r[0]}{r[1]}") if r else None
        v = value.strip()
        return (v, v) if v else None

    @staticmethod
    def _unit_of(normalizer: str, _val) -> str:
        return {NORM_AMOUNT: "元", NORM_DATETIME: "datetime",
                NORM_DURATION: "天"}.get(normalizer, "text")

    @staticmethod
    def _detect_conflicts(results: dict[str, FieldExtraction]) -> list[FieldConflict]:
        conflicts: list[FieldConflict] = []
        for f in results.values():
            distinct: dict[str, FieldCandidate] = {}
            for c in f.candidates:
                k = str(c.value_normalized)
                if k not in distinct or c.confidence > distinct[k].confidence:
                    distinct[k] = c
            if len(distinct) > 1:
                chosen = max(distinct.values(), key=lambda c: c.confidence)
                conflicts.append(FieldConflict(
                    field_key=f.field_key, chosen=str(chosen.value_normalized),
                    alternatives=sorted(distinct.values(),
                                        key=lambda c: -c.confidence),
                    note="多个来源取值不一致：已按置信度取值，请人工确认"))
        return conflicts


def _fmt_number(v) -> str:
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)
