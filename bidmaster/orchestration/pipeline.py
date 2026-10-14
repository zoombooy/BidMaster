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
from bidmaster.schemas.evidence import EvidenceStore, make_evidence
from bidmaster.schemas.fields import STATUS_FOUND
from bidmaster.schemas.report import (AnchorZone, LedgerSummary, SectionNode,
                                      TenderReport)
from bidmaster.scoring.requirements import RequirementBuilder
from bidmaster.scoring.table_scores import extract_score_items
from bidmaster.scoring.validate import required_fields_check, validate_scores
from bidmaster.storage.json_store import read_json, write_json, workspace_for
from bidmaster.structure.anchors import find_anchor_zones
from bidmaster.structure.toc import build_section_tree, flatten

def _fe_get(f, key: str) -> str:
    """字段对象/dict 统一取值（模型对象与 stage JSON 两态兼容）——模块级，
    供 _stage_fields 与 _stage_report 共用（局部函数曾导致越界 NameError）。"""
    if f is None:
        return ""
    v = f.get(key) if isinstance(f, dict) else getattr(f, key, "")
    return v or ""


STAGE_FILES = {
    "intake": "01_intake.json",
    "parse": "02_parsed.json",
    "structure": "03_structure.json",
    "fields": "04_fields.json",
    "scoring": "05_scoring.json",
    "report": "06_report.json",
}
# 产物格式版本：解析/评分逻辑升级导致旧缓存不兼容时必须 bump，
# 管线启动时检测版本不符会自动级联失效全部缓存（防止新旧逻辑结果混杂）。
CODE_VERSION = "0.5.6"
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
    def run(self, file_paths: str | list[str], *, force: bool = False,
            use_llm: bool = True) -> TenderReport:
        """支持单文件或多个文件（公告+正文）；zip 自动递归解压展开。"""
        files = [file_paths] if isinstance(file_paths, str) else list(file_paths)
        files = self._expand_archives(files)
        ws, doc_id = self._ensure_workspace(files, force)
        meta = self._stage_intake(ws, doc_id, files, force)
        parsed = self._stage_parse(ws, meta, force)
        struct = self._stage_structure(ws, parsed, force)
        fields, conflicts, lot_fields, lot_info = self._stage_fields(
            ws, parsed, struct, use_llm, force)
        scoring = self._stage_scoring(ws, parsed, struct, fields, force,
                                      use_llm=use_llm)
        report = self._stage_report(ws, meta, parsed, struct, fields, conflicts,
                                    scoring, force, lot_fields=lot_fields,
                                    lot_info=lot_info)
        return report

    def _expand_archives(self, files: list[str]) -> list[str]:
        """zip 输入 → 递归解压 → 收集全部文档（含 xlsx）。"""
        from bidmaster.ingestion.archive import collect_documents, extract_zip
        out: list[str] = []
        for fp in files:
            if Path(fp).suffix.lower() == ".zip":
                target = self.work_root / "_archives" / Path(fp).stem[:50]
                extract_zip(Path(fp), target)
                docs = collect_documents([target])
                if not docs:
                    raise ValueError(f"压缩包内无可解析文档: {fp}")
                out.extend(str(p) for p in docs)
            else:
                out.append(fp)
        return out

    def load_report(self, doc_id: str) -> TenderReport | None:
        p = self.work_root / doc_id / STAGE_FILES["report"]
        if not p.exists():
            return None
        return TenderReport.model_validate(read_json(p))

    # ---------- 工作区 ----------
    def _ensure_workspace(self, file_paths: list[str], force: bool):
        infos = [intake_mod.intake(Path(f)) for f in file_paths]
        import hashlib
        combined = hashlib.sha256(
            "|".join(i["sha256"] for i in infos).encode()).hexdigest()
        doc_id = combined[:12]
        ws = workspace_for(self.work_root, doc_id)
        self._invalidate_stale_cache(ws, force)
        for i, info in enumerate(infos):
            src = ws / f"source_{i}{Path(file_paths[i]).suffix.lower()}"
            if not src.exists():
                shutil.copy(file_paths[i], src)
        return ws, doc_id

    def _invalidate_stale_cache(self, ws: Path, force: bool) -> None:
        """代码版本变更 → 旧产物全部失效（防止新旧逻辑结果混杂）；
        --force → 手动级联失效。"""
        stages = ["parse", "structure", "fields", "scoring", "report"]
        intake_file = ws / STAGE_FILES["intake"]
        stale = force
        if not stale and intake_file.exists():
            try:
                stale = read_json(intake_file).get("code_version") != CODE_VERSION
            except Exception:  # noqa: BLE001 损坏文件视同过期
                stale = True
        if stale:
            # evidence.json 一并失效：证据登记表由各阶段重建，
            # 残留会把上一次运行（可能换了上传文件名）的旧证据带进新报告。
            # intake 也要失效：否则旧 intake 里过期的 code_version 会让后续
            # 每次运行都误判缓存过期，同一文件永远全量重跑（_stage_intake
            # 在文件缺失时会重建并写入当前 CODE_VERSION）。
            for name in [STAGE_FILES[s] for s in stages] + [STAGE_FILES["intake"],
                                                            "ledger.json",
                                                            "evidence.json"]:
                f = ws / name
                if f.exists():
                    f.unlink()

    # ---------- 各阶段 ----------
    def _stage_intake(self, ws: Path, doc_id: str, file_paths: list[str],
                      force: bool) -> dict:
        f = ws / STAGE_FILES["intake"]
        if f.exists() and not force:
            return read_json(f)
        infos = [intake_mod.intake(Path(p)) for p in file_paths]
        for info, p in zip(infos, file_paths):
            info["doc_id"] = doc_id
            info["path"] = str(Path(p).resolve())
        meta = {
            "doc_id": doc_id,
            "file_name": " + ".join(Path(p).name for p in file_paths),
            "files": infos,
            "sha256": infos[0]["sha256"] if len(infos) == 1 else "",
            "code_version": CODE_VERSION,
        }
        write_json(f, meta)
        return meta

    def _stage_parse(self, ws: Path, meta: dict, force: bool) -> ParsedDocument:
        f = ws / STAGE_FILES["parse"]
        if f.exists() and not force:
            return ParsedDocument.model_validate(read_json(f))
        ledger = ProcessingLedger(doc_id=meta["doc_id"])
        docs: list[ParsedDocument] = []
        for i, info in enumerate(meta["files"]):
            probe = info.get("probe") or {}
            try:
                doc = route_and_parse(info["path"], info["doc_id"],
                                      info["routed_type"], ledger,
                                      empty_pages=probe.get("empty_pages"))
            except NotImplementedError as e:
                # .doc 等需预转换格式：记账本转人工，不阻塞整包
                hint = "请用 Word/WPS 打开该文件，【另存为】.docx（直接改扩展名无效）后重新解析"
                ledger.register(f"file:{i}", "file")
                ledger.fail(f"file:{i}", f"{e}；{hint}")
                continue
            except ValueError as e:
                ledger.register(f"file:{i}", "file")
                ledger.fail(f"file:{i}", f"不支持的类型: {e}")
                continue
            doc.quality.warnings.extend(self._quality_warnings(info))
            docs.append(doc)
        parsed = docs[0] if len(docs) == 1 else self._merge_parsed(meta, docs)
        # 来源标注：单文件时全部块/表挂该文件名（与多文件合并的标注对称）
        if len(docs) == 1 and parsed.blocks:
            fname = docs[0].file_name
            for b in parsed.blocks:
                if not b.source_file:
                    b.source_file = fname
            for t in parsed.tables:
                if not t.source_file:
                    t.source_file = fname
        parsed.sha256 = meta.get("sha256", "")

        # DOCX 页码锚定：有 LibreOffice 时转 PDF 并回写块页码（证据定位用）。
        # 单文件 docx 与 multi 合并（逐个源文件锚定各自块）都覆盖；
        # 源文件路径从 intake meta 取（本方法签名里没有 file_paths）。
        if parsed.file_type in ("docx", "multi"):
            try:
                from bidmaster.parser.page_anchor import anchor_blocks, docx_to_pdf
                src_paths = [fi.get("path") or fi.get("file_name", "")
                             for fi in meta.get("files", [])]
                for fi, fp in enumerate(src_paths):
                    if not fp or Path(fp).suffix.lower() != ".docx":
                        continue
                    pdf_path = docx_to_pdf(fp, ws / "_pdf")
                    if not pdf_path:
                        continue
                    # 重命名对齐源副本命名：原文页接口按 source_N 查预览
                    # （docx_to_pdf 用的是原始文件名，两者对不上会白转）
                    wanted = ws / "_pdf" / f"source_{fi}.pdf"
                    if Path(pdf_path) != wanted:
                        try:
                            Path(pdf_path).replace(wanted)
                            pdf_path = wanted
                        except OSError:
                            pass
                    doc_blocks = [b for b in parsed.blocks
                                  if b.source_file == Path(fp).name]
                    n = anchor_blocks(doc_blocks, pdf_path)
                    # 表格页码同步：表格证据取的是表级页码，锚定只写块——
                    # 由成员块的页码汇总回写 TableModel.page_nos（跨页表多个页码）
                    tbl_pages: dict[str, set[int]] = {}
                    for b2 in doc_blocks:
                        if b2.table_ref and b2.page_no:
                            tbl_pages.setdefault(b2.table_ref, set()).add(b2.page_no)
                    for t2 in parsed.tables:
                        if (t2.source_file == Path(fp).name
                                and t2.table_id in tbl_pages):
                            t2.page_nos = sorted(tbl_pages[t2.table_id])
                    if n:
                        print(f"[页码锚定] {Path(fp).name}: {n} 块已定位页码")
            except Exception as exc:  # noqa: BLE001  页码锚定失败不影响主链
                print(f"[页码锚定] 失败（不影响主链）: {exc}")
        write_json(ws / "ledger.json",
                   {**ledger.model_dump(mode="json"), "summary": ledger.summary()})
        write_json(f, parsed.model_dump(mode="json"))
        return parsed

    @staticmethod
    def _merge_parsed(meta: dict, docs: list[ParsedDocument]) -> ParsedDocument:
        """多文件合并：块顺序拼接（字符偏移顺延）、表格重编号、文件名标注。"""
        from bidmaster.schemas.document import Block, ParsedDocument, TableModel
        blocks: list[Block] = []
        tables = []
        char_base = 0
        t_seq = 0
        b_seq = 0
        id_map: dict[str, str] = {}
        for i, doc in enumerate(docs):
            tag = f"[文件{i + 1}:{doc.file_name}]"
            b = doc.blocks[0].model_copy(update={
                "block_id": f"b-{b_seq + 1:05d}", "type": "heading",
                "text": tag,
                "style": doc.blocks[0].style.model_copy(update={"heading_level": 1}),
                "char_start": char_base, "char_end": char_base + len(tag)})
            b_seq += 1
            blocks.append(b.model_copy(update={"source_file": doc.file_name}))
            char_base += len(tag) + 1
            # 合并全文 = 各块文本以单换行连接（见下方 "\n".join），
            # 块偏移按运行位置重算——原块偏移基于各文档自身全文，
            # 直接累加会把文档内块间隙重复计入（偏移漂移、证据回查错位）
            pos = char_base
            for blk in doc.blocks:
                new_ref = blk.table_ref
                if blk.table_ref:
                    t_seq += 1
                    new_ref = f"t-{t_seq:04d}"
                    id_map[(i, blk.table_ref)] = new_ref
                b_seq += 1
                tlen = len(blk.text or "")
                blocks.append(blk.model_copy(update={
                    "block_id": f"b-{b_seq:05d}",
                    "source_file": doc.file_name,
                    "char_start": pos,
                    "char_end": pos + tlen,
                    "table_ref": new_ref}))
                pos += tlen + 1
            char_base = pos
            for t in doc.tables:
                t_seq += 1
                new_id = id_map.get((i, t.table_id), t.table_id)
                tables.append(t.model_copy(update={"table_id": new_id,
                                                   "source_file": doc.file_name}))
        merged = ParsedDocument(
            doc_id=meta["doc_id"], file_name=meta["file_name"],
            file_type="multi", pages=[p for d in docs for p in d.pages],
            blocks=blocks, tables=tables,
            full_text="\n".join(b.text for b in blocks),
        )
        merged.quality.warnings.append(
            "多文件合并解析：page_no 为各文件内页码；字段冲突可能来自不同文件（按优先级合并）")
        return merged


    @staticmethod
    def _backfill_evidence_sources(parsed: ParsedDocument, store) -> None:
        """回填证据 source_file：block_id/table_id 直查，
        其余用 snippet 在各源文件聚合文本中查找（块间缝隙不漏）。"""
        bmap = {b.block_id: b.source_file for b in parsed.blocks if b.source_file}
        tmap = {t.table_id: t.source_file for t in parsed.tables if t.source_file}
        # 按源文件聚合文本：snippet 查找用
        file_texts: dict[str, str] = {}
        for b in parsed.blocks:
            if b.source_file and b.text:
                prev = file_texts.get(b.source_file, "")
                file_texts[b.source_file] = prev + "\n" + b.text
        for ev in store.items:
            if ev.source_file:
                continue
            if ev.block_id and ev.block_id in bmap:
                ev.source_file = bmap[ev.block_id]
                continue
            if ev.table_id and ev.table_id in tmap:
                ev.source_file = tmap[ev.table_id]
                continue
            # 兜底：snippet 前缀在哪个源文件的文本中（LLM quote 偏移可能与合并坐标错位）
            snip = (ev.snippet or "")[:15]
            if snip:
                for sf, ftext in file_texts.items():
                    if snip in ftext:
                        ev.source_file = sf
                        break

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
            "chunks": [c.__dict__ for c in chunks],
        }
        write_json(f, data)
        return data

    def _stage_fields(self, ws: Path, parsed: ParsedDocument, struct: dict,
                      use_llm: bool, force: bool):
        f = ws / STAGE_FILES["fields"]
        if f.exists() and not force:
            data = read_json(f)
            return (data["fields"], [c for c in data.get("conflicts", [])],
                    data.get("lot_fields", {}), data.get("lot_info", []))
        store = self._load_store(ws)
        zones = [AnchorZone.model_validate(z) for z in struct["zones"]]
        llm = LLMClient() if use_llm else None
        extractor = FieldExtractor(parsed, zones, store, llm)
        fields, conflicts = extractor.extract_all(use_llm=use_llm)
        fields_d = {k: v.model_dump(mode="json") for k, v in fields.items()}
        conflicts_d = [c.model_dump(mode="json") for c in conflicts]

        # —— P0 范围解析：分标/包件作用域字段 ——
        from bidmaster.structure.lot_scope import (infer_file_scope,
                                                   extract_lot_table_fields,
                                                   file_block_ranges)
        lot_fields: dict[str, dict] = {}
        lot_info: list[dict] = []

        # 1) 公告逐包限价/工期表 → per-lot 字段
        for lot, bucket in extract_lot_table_fields(parsed, store).items():
            lot_fields.setdefault(lot, {}).update(bucket)

        # 2) 文件级作用域：包件专属文件（如 结算审核包1新.docx）→ 对该文件的
        #    块区间跑一次字段抽取，结果只归属该包件（质量要求/工期等不串包）
        for start, end, fname in file_block_ranges(parsed):
            scope = infer_file_scope(fname, parsed.blocks[start:end])
            if not scope:
                continue
            lot = scope["lot"]
            lot_info.append({"lot": lot, "project": scope.get("project", ""),
                             "source_file": fname})
            sub = parsed.model_copy(deep=True)
            sub.blocks = parsed.blocks[start + 1:end]
            sub.tables = [t for t in parsed.tables
                          if any(b.table_ref == t.table_id
                                 for b in sub.blocks if b.table_ref)]
            sub.full_text = "\n".join(b.text for b in sub.blocks)
            sub_ex = FieldExtractor(sub, [], store, llm)
            n_before = len(store.items)
            sub_fields, _ = sub_ex.extract_all(use_llm=use_llm)
            # 子抽取 llm_quote 的字符偏移基于子文档全文，与主文档全文不一致，
            # 证据定位会错位——按子块在主文档中的运行位置重映射回主文档坐标
            # （子块是主块副本，其 char_start 即主文档坐标；子全文同为单换行连接）
            sub_spans = []
            p2 = 0
            for b2 in sub.blocks:
                t2 = len(b2.text or "")
                sub_spans.append((p2, p2 + t2, b2.char_start))
                p2 += t2 + 1
            for ev in store.items[n_before:]:
                if not ev.source_file:
                    ev.source_file = fname
                if ev.kind != "llm_quote" or ev.char_end <= ev.char_start:
                    continue
                # 起止分别定位所在子块再映射：引文跨段时终点在后面的块里，
                # 截断到起点所在块会丢掉跨段部分（主文档坐标连续，跨块区间完整）
                def _to_main(offset: int) -> int | None:
                    for (s0, e0, main0) in sub_spans:
                        if s0 <= offset < e0:
                            return main0 + (offset - s0)
                    return None
                m_start = _to_main(ev.char_start)
                m_end = _to_main(ev.char_end - 1)
                if m_start is not None and m_end is not None:
                    ev.char_start = m_start
                    ev.char_end = m_end + 1
            bucket = lot_fields.setdefault(lot, {})
            for k, fv in sub_fields.items():
                if fv.status == STATUS_FOUND and k not in bucket:
                    fv.scope = lot
                    bucket[k] = fv.model_dump(mode="json")
            # 包件项目名：来自文件标题前缀（"浙江特高压交流环网线路工程"），
            # 优先于子抽取结果（后者可能抓到工程分类行）
            if scope.get("project"):
                from bidmaster.schemas.fields import FieldExtraction as _FE
                proj_ev = store.add(make_evidence(
                    kind="block", source="rules", page_no=0,
                    snippet=f"{scope['project']}（来源：{fname}）"))
                proj_ev.source_file = fname
                bucket["project_name"] = _FE(
                    field_key="project_name", scope=lot, field_label="项目名称（包件）",
                    status=STATUS_FOUND, value_raw=scope["project"],
                    value_normalized=scope["project"], confidence=0.9,
                    evidence_ids=[proj_ev.evidence_id]).model_dump(mode="json")

        # 3) 包清单并集：公告表格抽出的包若不在文件级包清单里，补进 lot_info。
        # 同包判定用"包号数字相等"（包1 与 结算审核包1 同包；与 包10 不同——
        # 子串包含判断会把包1 误判成已存在于 结算审核包10 造成漏包）。
        # 命中已有条目时把表格字段重键并入规范名，导出按 lot_info 名才查得到。
        import re as _re

        def _lot_num(lot: str) -> int | None:
            from bidmaster.summary.scope import package_numbers, split_fenbiao
            ns = package_numbers(split_fenbiao(lot)[1])
            return min(ns) if ns else None

        def _canonical(lot: str) -> str | None:
            num = _lot_num(lot)
            fb = (lot or "").split("::", 1)[0] if "::" in (lot or "") else ""
            for info in lot_info:
                nm = info.get("lot", "")
                if lot == nm:
                    return nm
                nfb = (nm or "").split("::", 1)[0] if "::" in (nm or "") else ""
                # 只要一方带分标：分标不同或对方缺失都不合并（缺分标依据，
                # 凭包号数字合并会让跨分标同号包互相覆盖字段）
                if (fb or nfb) and fb != nfb:
                    continue
                if num is not None and _lot_num(nm) == num:
                    return nm
            return None

        for lot in list(lot_fields.keys()):
            canon = _canonical(lot)
            if canon is None:
                bucket = lot_fields[lot]
                # 项目名称只取真实抽取的项目名称列；包名称不再冒充项目名称
                pkg = bucket.get("project_name")
                lot_info.append({"lot": lot,
                                 "project": _fe_get(pkg, "value_normalized"),
                                 "source_file": "公告包件表"})
                continue
            if canon == lot:
                continue
            tgt = lot_fields.setdefault(canon, {})
            for k, v in lot_fields[lot].items():
                tgt.setdefault(k, v)
            del lot_fields[lot]

        self._backfill_evidence_sources(parsed, store)

        write_json(ws / "evidence.json", store.model_dump(mode="json"))
        write_json(f, {"fields": fields_d, "conflicts": conflicts_d,
                       "lot_fields": lot_fields, "lot_info": lot_info})
        return fields_d, conflicts_d, lot_fields, lot_info

    def _stage_scoring(self, ws: Path, parsed: ParsedDocument, struct: dict,
                       fields: dict, force: bool, use_llm: bool = True) -> dict:
        f = ws / STAGE_FILES["scoring"]
        if f.exists() and not force:
            return read_json(f)
        store = self._load_store(ws)
        zones = [AnchorZone.model_validate(z) for z in struct["zones"]]
        from bidmaster.schemas.report import SectionNode
        sections = [SectionNode.model_validate(s) for s in struct.get("sections", [])]
        scores, declared = extract_score_items(parsed, zones, store, sections=sections)
        builder = RequirementBuilder(parsed, zones, scores, store)
        reqs, stars = builder.build()
        from bidmaster.scoring.rejection import extract_rejections
        rejections = extract_rejections(parsed, store)
        # LLM 兜底：散文式评分细则（如价格公式 blob）结构化——需配置 Key 才生效
        if use_llm:
            from bidmaster.llm.client import LLMClient
            from bidmaster.scoring.llm_scores import refine_pending_scores
            llm = LLMClient()
            if llm.enabled:
                upgraded = refine_pending_scores(parsed, scores, store, llm)
                if upgraded:
                    print(f"[llm] 评分细则结构化升级 {upgraded} 项")
        # 评分阶段新增的证据同样回填 source_file（含 LLM quote 字符定位）
        self._backfill_evidence_sources(parsed, store)
        write_json(ws / "evidence.json", store.model_dump(mode="json"))
        data = {
            "scores": [s.model_dump(mode="json") for s in scores],
            "requirements": [r.model_dump(mode="json") for r in reqs],
            "stars": [s.model_dump(mode="json") for s in stars],
            "rejections": [r.model_dump(mode="json") for r in rejections],
            "declared": declared,
        }
        write_json(f, data)
        return data

    def _stage_report(self, ws: Path, meta: dict, parsed: ParsedDocument,
                      struct: dict, fields: dict, conflicts: list,
                      scoring: dict, force: bool,
                      lot_fields: dict | None = None,
                      lot_info: list | None = None) -> TenderReport:
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
        # 投标文件构成目录（"第X章 投标文件格式"标题树 + 评分项补充）与单包聚焦。
        # 目标包先定：目录补充使用统一范围模型过滤后的评分项
        # （项目通用/分标通用/目标包专属保留，仅明确属于其他包的排除）
        from bidmaster.summary.outline import extract_bid_format_outline, pick_target_lot
        from bidmaster.summary.scope import filter_for_target
        score_items = [s for s in (scoring["scores"])]
        target_lot = pick_target_lot(lot_info or [])
        scores_for_outline = score_items
        if target_lot:
            scores_for_outline, _dropped = filter_for_target(score_items, target_lot)
        from bidmaster.summary.scope import scope_pending
        pending_ids = {_fe_get(s, "score_id")
                       for s in scope_pending(score_items, target_lot)}
        bid_outline = extract_bid_format_outline(parsed, scores_for_outline,
                                                 pending_ids=pending_ids)
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
            rejections=scoring.get("rejections", []),
            lot_fields=lot_fields or {},
            lot_info=lot_info or [],
            target_lot=target_lot,
            bid_outline=bid_outline,
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
                "lots": len(lot_fields or {}),
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
