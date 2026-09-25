"""评分表解析 + 要求建模 + 分值校验测试。"""
from bidmaster.parser.tables import merge_cross_page_tables
from bidmaster.schemas.document import TableModel
from bidmaster.schemas.evidence import EvidenceStore
from bidmaster.schemas.fields import empty_extraction
from bidmaster.schemas.report import AnchorZone
from bidmaster.scoring.requirements import RequirementBuilder
from bidmaster.scoring.table_scores import extract_score_items
from bidmaster.scoring.validate import validate_scores

SCORE_ROWS = [
    ["序号", "评分项", "分值", "评分标准"],
    ["技术标（60分）", "", "", ""],
    ["1", "类似工程业绩", "6", "近5年每提供1项单项合同金额5000万元以上的同类工程业绩得2分，最多6分。需提供中标通知书、合同协议书、竣工验收证明。"],
    ["2", "施工组织设计", "20", "施工方案完整可行：优16-20分，良12-15分。"],
    ["3", "项目负责人", "10", "项目负责人具备一级注册建造师且具有高级工程师职称的得4分；无在建工程得3分。"],
    ["4", "工作组人员配备", "14", "技术负责人具有高级工程师职称得4分；配备安全员2名且均具有安全生产考核合格证（B证）得5分。"],
    ["5", "质量保证与安全文明", "10", "质量保证体系健全、安全文明施工措施完善：优8-10分，良6-7分，一般4-5分，差0-3分。"],
    ["商务标（30分）", "", "", ""],
    ["5", "企业资质与认证", "8", "具有ISO9001质量管理体系认证得3分；ISO14001得2分；ISO45001得3分。"],
    ["6", "财务状况", "8", "提供近三年经审计的财务报告，每年得2分，最多6分。"],
    ["7", "企业信用", "6", "信用中国网站无失信被执行人记录得3分；无重大税收违法记录得3分。"],
    ["8", "企业荣誉", "8", "近3年获得省级及以上优质工程奖项每项得4分，最多8分。"],
    ["价格标（10分）", "", "", ""],
    ["9", "投标报价", "10", "价格分=（评标基准价/投标报价）×10。"],
]


def _mk_tables(rows) -> list[TableModel]:
    return [TableModel(table_id="t-0001", page_nos=[45], rows=rows, header_rows=1)]


def _mk_parsed(tables):
    from bidmaster.schemas.document import Block, ParsedDocument
    blocks = [Block(block_id="b-000", type="heading",
                    text="第三章 评标办法 评分细则")]
    for i, t in enumerate(tables, 1):
        blocks.append(Block(block_id=f"b-{i:03d}", type="table",
                            text="\n".join("\t".join(r) for r in t.rows),
                            table_ref=t.table_id))
    return ParsedDocument(doc_id="t", file_name="t.docx", file_type="docx",
                          blocks=blocks, tables=tables)


ZONES = [AnchorZone(zone="scoring", block_start=0, block_end=5),
         AnchorZone(zone="evaluation_method", block_start=0, block_end=5)]


class TestScoreTable:
    def test_extract_items(self):
        store = EvidenceStore(doc_id="t")
        tables = _mk_tables(SCORE_ROWS)
        parsed = _mk_parsed(tables)
        items, declared = extract_score_items(parsed, ZONES, store)

        assert len(items) == 10
        assert declared == [{"category": "technical", "total": 60.0, "source": "t-0001 r1"},
                            {"category": "commercial", "total": 30.0, "source": "t-0001 r7"},
                            {"category": "price", "total": 10.0, "source": "t-0001 r12"}]
        perf = next(i for i in items if i.name == "类似工程业绩")
        assert perf.score_id == "T-001" and perf.max_score == 6
        assert perf.parsed_rule.get("years") == 5
        assert perf.parsed_rule.get("unit_score") == 2
        assert perf.parsed_rule.get("max_score_cap") == 6
        assert perf.parsed_rule.get("amount_min") == 50_000_000
        assert "中标通知书" in perf.evidence_required
        price = items[-1]
        assert price.category == "price" and price.rule_type == "formula"
        assert price.formula and "评标基准价" in price.formula

    def test_sum_validation(self):
        store = EvidenceStore(doc_id="t")
        parsed = _mk_parsed(_mk_tables(SCORE_ROWS))
        items, declared = extract_score_items(parsed, ZONES, store)
        issues: list[str] = []
        checks = validate_scores(items, declared, {}, issues)
        assert all(c.ok for c in checks)
        assert sum(c.computed_total for c in checks) == 100
        assert not issues

    def test_mismatch_flagged(self):
        items = _mk_tables(SCORE_ROWS)
        broken = [r for r in SCORE_ROWS if r[0] != "2"]  # 删一行 → 合计≠声明
        store = EvidenceStore(doc_id="t")
        parsed = _mk_parsed(_mk_tables(broken))
        got, declared = extract_score_items(parsed, ZONES, store)
        issues: list[str] = []
        checks = validate_scores(got, declared, {}, issues)
        assert not all(c.ok for c in checks)
        assert issues  # 机械校验必须发现问题


class TestRequirements:
    def test_build(self):
        store = EvidenceStore(doc_id="t")
        tables = _mk_tables(SCORE_ROWS)
        parsed = _mk_parsed(tables)
        scores, _ = extract_score_items(parsed, ZONES, store)
        builder = RequirementBuilder(parsed, ZONES, scores, store)
        reqs, stars = builder.build()

        types = {r.type for r in reqs}
        assert "performance" in types
        assert "leader" in types
        assert "team" in types
        assert "qualification" in types
        assert "finance" in types
        assert "credit" in types
        assert "honor" in types
        assert "price" in types

        perf = next(r for r in reqs if r.type == "performance")
        assert perf.parsed.get("years") == 5
        assert perf.parsed.get("amount_min") == 50_000_000

        team = [r for r in reqs if r.type == "team"]
        subjects = {r.subject for r in team}
        assert "技术负责人" in subjects
        assert "安全员" in subjects
        safety = next(r for r in team if r.subject == "安全员")
        assert safety.parsed.get("count") == 2

        # 双向关联
        linked = [r for r in reqs if r.scoring_refs]
        assert linked
        scored = [s for s in scores if s.requirement_refs]
        assert scored
        assert stars == []  # 样例表内无★


class TestCrossPageMerge:
    def test_merge(self):
        t1 = TableModel(table_id="t-1", page_nos=[45], rows=[["评分项", "分值"], ["业绩", "6"], ["方案", ""]])
        t2 = TableModel(table_id="t-2", page_nos=[46], rows=[["", "续"], ["信用", "6"]])
        merged = merge_cross_page_tables([t1, t2])
        assert len(merged) == 1
        assert merged[0].page_nos == [45, 46]
        assert len(merged[0].rows) == 5

    def test_no_merge_when_cols_differ(self):
        t1 = TableModel(table_id="t-1", page_nos=[45], rows=[["a", "b"], ["c", ""]])
        t2 = TableModel(table_id="t-2", page_nos=[46], rows=[["x", "y", "z"]])
        assert len(merge_cross_page_tables([t1, t2])) == 2


class TestFieldHelpers:
    def test_empty_extraction(self):
        f = empty_extraction("price_limit")
        assert f.required is True and f.status == "not_found"
