"""第三轮升级测试：分层置信度模式库 / list_lots / get_report 过滤分页。"""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bidmaster.extraction import validators as V
from bidmaster.extraction.fields import FieldExtractor
from bidmaster.extraction.rules import FIELD_RULES, _squeeze_ocr_spaces
from bidmaster.schemas.document import Block, ParsedDocument
from bidmaster.schemas.evidence import EvidenceStore


def _extract(key: str, text: str, zone: str | None = "notice"):
    blocks = [Block(block_id="b-1", text=text)]
    parsed = ParsedDocument(doc_id="t", file_name="t", file_type="docx",
                            blocks=blocks, full_text=text)
    from bidmaster.schemas.report import AnchorZone
    zones = [AnchorZone(zone=zone, block_start=0, block_end=1)] if zone else []
    ex = FieldExtractor(parsed, zones, EvidenceStore(doc_id="t"), None)
    return ex.extract_one(key)


class TestLayeredPatterns:
    def test_pattern_layers(self):
        """同字段多模式分层：各层形态都能命中对应值。"""
        f1 = _extract("price_limit", "最高投标限价（人民币）：¥86,000,000 元。")
        assert f1.value_normalized == "86000000"   # 括号形态 0.98
        f2 = _extract("price_limit", "最高限价：人民币捌仟陆佰万元整")
        assert f2.value_normalized == "86000000"   # 大写形态 0.95
        f3 = _extract("price_limit", "招标控制价：10,000,000 元")
        assert f3.value_normalized == "10000000"   # 数字形态 0.93

    def test_layered_beats_generic(self):
        f = _extract("security_deposit", "投标保证金：人民币伍拾万元整（¥5,000,000.00）。")
        assert f.value_normalized == "500000"   # 伍拾万元 = 50 万元
        assert f.confidence >= 0.70  # 全文来源×模式置信 = 0.78×0.93 ≈ 0.73

    def test_ocr_space_fix_in_layered(self):
        f = _extract("tender_no", "招标编号：S C A N - 2 0 2 6 - G C - 0 0 9 9", zone=None)
        assert f.status == "found"
        assert " " not in (f.value_normalized or "")

    def test_all_fields_have_patterns(self):
        from bidmaster.schemas.fields import FIELD_CATALOG
        missing = [k for k in FIELD_CATALOG if k not in FIELD_RULES]
        assert not missing, f"字段缺规则包: {missing}"


class TestLotsTool:
    def test_list_lots_from_report(self, tmp_path: Path, monkeypatch):
        from bidmaster import mcp_server
        from bidmaster.schemas.scoring import ScoreItem
        # 伪造两分标报告
        report = {
            "doc_id": "docL", "file_name": "multi.zip", "stats": {},
            "scores": [
                {"score_id": "T-001", "category": "technical", "name": "业绩",
                 "max_score": 10, "parsed_rule": {"lot": "换流站土建"}},
                {"score_id": "T-002", "category": "technical", "name": "方案",
                 "max_score": 20, "parsed_rule": {"lot": "变电站土建"}},
            ],
        }
        ws = tmp_path / "work" / "docL"
        ws.mkdir(parents=True)
        (ws / "06_report.json").write_text(json.dumps(report), encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        result = json.loads(mcp_server._tool_list_lots({"doc_id": "docL"}))
        assert result["lot_count"] == 2
        lots = {l["lot"]: l for l in result["lots"]}
        assert lots["换流站土建"]["score_items"] == 1
        assert "hint" in result  # 多分标提示


class TestGetReportFilter:
    @pytest.fixture
    def client(self, tmp_path: Path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        from bidmaster.api.main import app
        with TestClient(app) as c:
            yield c, tmp_path

    def _seed(self, tmp_path: Path):
        report = {"doc_id": "docF", "file_name": "f.docx", "stats": {},
                  "fields": {}, "scores": [
                      {"score_id": "T-001", "category": "technical", "name": "业绩",
                       "max_score": 10, "parsed_rule": {"lot": "A分标"}},
                      {"score_id": "T-002", "category": "commercial", "name": "资质",
                       "max_score": 5, "parsed_rule": {"lot": "A分标"}}],
                  "requirements": [], "rejections": [{"rej_id": "RJ-001",
                                                      "category": "主体不符", "text": "x"}],
                  "sum_checks": [], "evidence": [], "issues": [],
                  "conflicts": [], "star_clauses": []}
        ws = tmp_path / "work" / "docF"
        ws.mkdir(parents=True)
        (ws / "06_report.json").write_text(json.dumps(report, ensure_ascii=False),
                                           encoding="utf-8")

    def test_filter_by_category(self, client, tmp_path):
        c, _ = client
        self._seed(tmp_path)
        r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                 "params": {"name": "get_report", "arguments": {
                                     "doc_id": "docF", "category": "technical",
                                     "section": "scores"}}}).json()
        scores = r["result"]["content"][0]
        data = json.loads(scores["text"]) if "text" in scores else scores
        assert data["scores_total"] == 1
        assert data["scores"][0]["category"] == "technical"

    def test_section_rejections(self, client, tmp_path):
        c, _ = client
        self._seed(tmp_path)
        r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                 "params": {"name": "get_report", "arguments": {
                                     "doc_id": "docF", "section": "rejections"}}}).json()
        data = json.loads(r["result"]["content"][0]["text"])
        assert data["rejections"][0]["rej_id"] == "RJ-001"


class TestTypedCleaning:
    """三类实战噪声用例（对照 tender-extract 清洗器/合并器架构的回归）。"""

    def _extract(self, key, text, zone=None):
        from bidmaster.schemas.document import Block, ParsedDocument
        from bidmaster.schemas.evidence import EvidenceStore
        from bidmaster.schemas.report import AnchorZone
        blocks = [Block(block_id="b-1", text=text)]
        parsed = ParsedDocument(doc_id="t", file_name="t", file_type="docx",
                                blocks=blocks, full_text=text)
        zones = [AnchorZone(zone=zone, block_start=0, block_end=1)] if zone else []
        ex = FieldExtractor(parsed, zones, EvidenceStore(doc_id="t"), None)
        return ex.extract_one(key)

    from bidmaster.extraction.fields import FieldExtractor

    def test_deposit_number_rejected(self):
        """保证金收到非金额文本（勾选框说明）→ not_found。"""
        f = self._extract("security_deposit",
                          "投标保证金 → □本项目免收投标保证金，招标文件中保证金的相关要求均不适用。"
                          "☑本次招标要求投标人递交投标保证金，投标保")
        assert f.status != "found"

    def test_legal_rep_columnheader_rejected(self):
        """法定代表人收到列头"姓名" → not_found。"""
        f = self._extract("legal_representative", "姓名")
        assert f.status != "found"

    def test_credit_code_reference_rejected(self):
        """信用代码收到引用语"见招标公告" → not_found。"""
        f = self._extract("credit_code", "统一社会信用代码：见招标公告")
        assert f.status != "found"

    def test_reference_ok_for_org(self):
        """招标人字段引用语放行（"见招标公告"是合法的待查引用）。"""
        f = self._extract("tenderer", "招标人：见招标公告")
        assert f.status == "found"

    def test_deposit_real_value_still_works(self):
        """真金额仍正常：伍拾万元 → 500000。"""
        f = self._extract("security_deposit", "投标保证金：人民币伍拾万元整")
        assert f.status == "found" and f.value_normalized == "500000"
