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
        # 国网式权重表：'权重设置 技:商:价'（多分标每行一个三元组）
        self._weights_from_grid(results)
        # 标题式取值：'3 评标办法（综合评估法）'
        self._from_section_titles(results)
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
        return results, conflicts

    # ---------- 国网式 技:商:价 权重 ----------
    _WEIGHT_CELL = None  # 延迟编译见下

    def _weights_from_grid(self, results: dict[str, FieldExtraction]) -> None:
        import re as _re
        pat = _re.compile(r"(\d{1,3})\s*[：:]\s*(\d{1,3})\s*[：:]\s*(\d{1,3})")
        for tb in self.parsed.tables:
            header = "".join(tb.rows[0]) if tb.rows else ""
            if "技" not in header or "商" not in header:
                # 权重列也可能没有表头指引，退化为全表扫描
                pass
            for ri, row in enumerate(tb.rows):
                for ci, cell in enumerate(row):
                    m = pat.search(cell)
                    if not m:
                        continue
                    tech, comm, price = m.group(1), m.group(2), m.group(3)
                    ev = self.store.add(make_evidence(
                        kind="table_cell", source="table",
                        page_no=(tb.page_nos[0] if tb.page_nos else 0),
                        table_id=tb.table_id, row=ri, col=ci,
                        snippet=f"技:商:价 = {tech}:{comm}:{price}"))
                    for key, val in (("technical_weight", tech),
                                     ("commercial_weight", comm),
                                     ("price_weight", price)):
                        results[key].candidates.append(FieldCandidate(
                            value_raw=cell.strip(), value_normalized=val,
                            source="table", confidence=0.95,
                            evidence_id=ev.evidence_id, zone="权重表"))
                    # 有命中即升级该字段状态
                    for key in ("technical_weight", "commercial_weight", "price_weight"):
                        f = results[key]
                        best = max(f.candidates, key=lambda c: c.confidence)
                        f.status = STATUS_FOUND
                        f.value_raw = best.value_raw
                        f.value_normalized = best.value_normalized
                        f.confidence = best.confidence
                        f.evidence_ids = [best.evidence_id]
                    break  # 每行只取一次

    # ---------- 标题式取值 ----------
    def _from_section_titles(self, results: dict[str, FieldExtraction]) -> None:
        import re as _re
        pat = _re.compile(r"评标办法\s*[（(]([^）)]{2,20})[）)]")
        for b in self.parsed.blocks:
            if b.type != "heading":
                continue
            m = pat.search(b.text)
            if m and results["evaluation_method"].status != STATUS_FOUND:
                ev = self.store.add(make_evidence(
                    kind="block", source="rules", page_no=b.page_no, bbox=b.bbox,
                    block_id=b.block_id, snippet=b.text[:200]))
                f = results["evaluation_method"]
                cand = FieldCandidate(value_raw=m.group(1),
                                      value_normalized=m.group(1).strip(),
                                      source="rules", confidence=0.9,
                                      evidence_id=ev.evidence_id, zone="章节标题")
                f.candidates.append(cand)
                f.status = STATUS_FOUND
                f.value_raw = cand.value_raw
                f.value_normalized = cand.value_normalized
                f.confidence = cand.confidence
                f.evidence_ids = [cand.evidence_id]

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
        # 字段专属守卫
        candidates = [c for c in candidates if self._guard(field_key, c)]
        if not candidates:
            out.note = "候选值均未通过字段格式守卫"
            return out
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
        from bidmaster.extraction.rules import _is_junk_value
        for tb in self.parsed.tables:
            for ri, row in enumerate(tb.rows):
                if ri < tb.header_rows:
                    continue  # 表头行不做 label→值 查找（防"标包名称/工程规模"列头错位）
                for ci, cell in enumerate(row):
                    if not label_re.match(cell.strip().rstrip("：:")):
                        continue
                    value = self._cell_value(tb.rows, ri, ci)
                    if _is_junk_value(value):
                        continue
                    value = _strip_leading_label(value)
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
        """标签右侧取值；同行右侧为空则取同列下一行；跳过疑似列头的单元格。"""
        import re
        colheader = re.compile(r"^(?:[^0-9]{1,10}?)?(?:名称|编号|规模|金额|时间|等级|数量|算法|系数|内容|类型)(?:\s*[（(].*)?$")
        row = rows[ri]
        for cj in range(ci + 1, len(row)):
            v = row[cj].strip()
            if not v or v in ("—", "/", "无"):
                continue
            if colheader.match(v):  # 跨到下一列头了 → 停止
                break
            return v
        if ri + 1 < len(rows) and ci < len(rows[ri + 1]):
            v = rows[ri + 1][ci].strip()
            if v and v not in ("—", "/", "无") and not colheader.match(v):
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
    def _guard(field_key: str, cand: FieldCandidate) -> bool:
        """字段级格式守卫：结构不符的候选直接淘汰。"""
        import re
        v = str(cand.value_normalized or "")
        if field_key == "tender_no" and not re.search(r"[0-9A-Za-z]", v):
            return False  # 编号必含字母/数字（"异议人类型：□…"之类噪声）
        if field_key == "project_name" and v.rstrip("：:") in ("标包名称", "包名称", "分标"):
            return False
        if field_key == "security_deposit":
            try:
                if float(v) < 5000:
                    return False  # "1"这类值多为其他数字列错位，真实保证金≥数千元
            except ValueError:
                pass
        return True

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


def _strip_leading_label(value: str) -> str:
    """去掉值前的次级标签与首尾括号：'名称：某某医院' / '（0711-26OTL）' → 干净值。"""
    import re
    v = re.sub(r"^(?:名称|项目名称|工程名称|金额|大写)\s*[：:]\s*", "", value.strip())
    return v.strip("（）()").strip()
