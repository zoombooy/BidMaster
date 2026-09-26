"""P0 升级测试：精细校验器 / 新字段规则 / 废标清单 / MCP 路径白名单。"""
import json
from pathlib import Path

from bidmaster.extraction import validators as V
from bidmaster.schemas.document import ParsedDocument, TableModel
from bidmaster.schemas.evidence import EvidenceStore
from bidmaster.scoring.rejection import extract_rejections


class TestValidators:
    def test_id_card_checksum(self):
        assert V.is_valid_id_card("11010519491231002X")  # 标准测试号
        assert not V.is_valid_id_card("110105194912310021")  # 校验位错
        assert not V.is_valid_id_card("11010520491231002X")  # 出生年非法

    def test_credit_code(self):
        assert V.is_valid_credit_code("91310000MA1FL0XL2B")
        assert not V.is_valid_credit_code("91310000MA1FL0XL2IOS")  # 含 I O S 且长度错
        assert not V.is_valid_credit_code("123")

    def test_organization_and_meaningful(self):
        assert V.looks_like_organization("国网物资有限公司")
        assert V.looks_like_organization("某某市教育局")
        assert not V.looks_like_organization("见招标公告")
        assert not V.is_meaningful("详见")
        assert not V.is_meaningful("———————")

    def test_phone_and_person(self):
        assert V.is_valid_phone("010-88886666")
        assert V.is_valid_phone("13800138000")
        assert not V.is_valid_phone("0711-26OTL")
        assert V.is_person_name("张三丰")
        assert not V.is_person_name("国家电网有限公司")

    def test_numeric_spread(self):
        note = V.numeric_spread_note([18746000.0, 12850000.0, 4957500.0, 59.0])
        assert note and "差异过大" in note
        assert V.numeric_spread_note([100.0, 105.0]) is None


class TestGuards:
    def _extract_one(self, blocks, key):
        from bidmaster.extraction.fields import FieldExtractor
        from bidmaster.schemas.document import ParsedDocument
        from bidmaster.schemas.evidence import EvidenceStore
        parsed = ParsedDocument(doc_id="t", file_name="t.docx",
                                file_type="docx", blocks=blocks, full_text=blocks[0].text)
        ex = FieldExtractor(parsed, [], EvidenceStore(doc_id="t"), None)
        return ex.extract_one(key)

    def test_credit_code_field(self):
        from bidmaster.schemas.document import Block
        blocks = [Block(block_id="b-1", text="统一社会信用代码：91310000MA1FL0XL2B")]
        f = self._extract_one(blocks, "credit_code")
        assert f.status == "found" and f.value_normalized == "91310000MA1FL0XL2B"

    def test_legal_representative_rejects_company(self):
        from bidmaster.schemas.document import Block
        blocks = [Block(block_id="b-1", text="法定代表人：国网物资有限公司")]
        f = self._extract_one(blocks, "legal_representative")
        assert f.status == "not_found"


class TestRejection:
    def _parsed_with_rejection_table(self):
        from bidmaster.schemas.document import Block, ParsedDocument
        rows = [
            ["序号", "情形事项", "具体规定"],
            ["1", "主体不符", "（1）为招标人不具备独立法人资格的附属机构（单位）"],
            ["2", "资格不符", "不满足本次招标文件要求的投标人资格条件的"],
            ["3", "资信不良", "被责令停产停业、暂扣或者吊销许可证的"],
        ]
        tb = TableModel(table_id="t-0001", page_nos=[30], rows=rows, header_rows=1)
        blocks = [Block(block_id="b-001", type="table", page_no=30,
                        text="\n".join("\t".join(r) for r in rows), table_ref="t-0001")]
        return ParsedDocument(doc_id="t", file_name="t.docx", file_type="docx",
                              blocks=blocks, tables=[tb])

    def test_extract(self):
        store = EvidenceStore(doc_id="t")
        items = extract_rejections(self._parsed_with_rejection_table(), store)
        assert len(items) == 3
        assert items[0].category == "主体不符"
        assert all(i.severity == "high" and i.origin == "explicit" for i in items)
        assert all(i.evidence_ids for i in items)

    def test_empty_for_normal_table(self):
        store = EvidenceStore(doc_id="t")
        rows = [["序号", "评分项", "分值"], ["1", "业绩", "6"]]
        from bidmaster.schemas.document import Block, ParsedDocument, TableModel
        tb = TableModel(table_id="t-0002", page_nos=[1], rows=rows, header_rows=1)
        blocks = [Block(block_id="b-1", type="table", text="", table_ref="t-0002")]
        parsed = ParsedDocument(doc_id="t", file_name="t", file_type="docx",
                                blocks=blocks, tables=[tb])
        assert extract_rejections(parsed, store) == []


class TestMcpPathWhitelist:
    def test_whitelist(self, tmp_path: Path, monkeypatch):
        import os
        from bidmaster.mcp_server import _resolve_local_path
        monkeypatch.setenv("BIDMASTER_ALLOWED_PATHS", str(tmp_path))
        from bidmaster.config import get_settings
        get_settings().allowed_paths = [str(tmp_path)]
        ok = _resolve_local_path(str(tmp_path / "a.docx"))
        assert ok.is_absolute()
        import pytest
        with pytest.raises(ValueError):
            _resolve_local_path("/etc/passwd")
