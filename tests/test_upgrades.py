"""参考项目机制吸收后的新增能力测试：
zip 递归解压（GBK 名）/ 业绩时间窗 / 证书编号配对 / 梯度量化 / 价格公式矩阵 / xlsx 链路。"""
from __future__ import annotations

import zipfile
from pathlib import Path

from bidmaster.ingestion.archive import collect_documents, extract_zip
from bidmaster.schemas.document import TableModel
from bidmaster.schemas.evidence import EvidenceStore
from bidmaster.scoring.requirements import _perf_parse, _team_parse
from bidmaster.scoring.table_scores import (_parse_price_matrix,
                                            _parse_rule_numbers)


class TestArchive:
    def test_gbk_names_and_recursion(self, tmp_path: Path):
        # 模拟 Windows 工具打的 GBK 名 zip（无 UTF-8 标志）
        outer = tmp_path / "包1.zip"
        inner_bytes = _zip_bytes([("技术规范书.docx", b"INNER")])
        with zipfile.ZipFile(outer, "w") as z:
            info = zipfile.ZipInfo("第一章招标公告.docx")
            z.writestr(info, b"DOC1")
            z.writestr(zipfile.ZipInfo("招标文件.zip"), inner_bytes)
            z.writestr(zipfile.ZipInfo("bidpkg.sign"), b"SIGN")  # 应被忽略
        docs = extract_zip(outer, tmp_path / "out")
        names = sorted(p.name for p in docs)
        assert "第一章招标公告.docx" in names
        assert "技术规范书.docx" in names  # 递归解出的内层
        assert all(not n.endswith(".sign") for n in names)

    def test_collect(self, tmp_path: Path):
        (tmp_path / "a.docx").write_bytes(b"x")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "b.pdf").write_bytes(b"y")
        (tmp_path / "c.txt").write_text("no")
        names = [p.name for p in collect_documents([tmp_path])]
        assert names == ["a.docx", "b.pdf"]


def _zip_bytes(files: list[tuple[str, bytes]]) -> bytes:
    import io
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files:
            # 不设 UTF-8 标志 → 模拟 GBK 包名环境
            z.writestr(zipfile.ZipInfo(name), data)
    return buf.getvalue()


class TestRequirementParsing:
    def test_year_window_variants(self):
        for text in ("开标前5年内：企业具有±800kV换流站土建A包业绩的，得3分",
                     "投标截止日近3年完成的同类工程业绩",
                     "近5年完成2项单项合同金额5000万元以上业绩"):
            parsed = _perf_parse(text)
            assert parsed is not None, text
            assert parsed.get("years"), text

    def test_non_performance_sentence_rejected(self):
        # 获奖类句子不是业绩要求（业绩关键词缺失 → 不建模）
        assert _perf_parse("投标截止日近3年企业取得过国家级奖项") is None

    def test_amount_and_count(self):
        parsed = _perf_parse("开标前5年内完成单项合同金额5000万元以上的同类工程业绩2项")
        assert parsed["amount_min"] == 50_000_000
        assert parsed["count_min"] == 2

    def test_team_cert_number(self):
        parsed = _team_parse(
            "配备安全员2名，安全生产考核合格证书编号：赣建安B（2026）0001234", "安全员")
        assert parsed and parsed["count"] == 2
        assert any("0001234" in n for n in parsed.get("cert_numbers", []))


class TestGradesQuantify:
    def test_grades(self):
        parsed = _parse_rule_numbers(
            "施工方案完整可行：优16-20分，良12-15分，一般8-11分，差0-7分。")
        tiers = {g["tier"]: (g["min"], g["max"]) for g in parsed.get("grades", [])}
        assert tiers["优"] == (16, 20)
        assert tiers["一般"] == (8, 11)
        assert tiers["差"] == (0, 7)


class TestPriceMatrix:
    def test_parse(self):
        tb = TableModel(
            table_id="t-9001", page_nos=[60],
            rows=[
                ["序号", "工程名称", "分标 名称", "权重设置 技:商:价", "价格计分算法",
                 "价格计分算法", "价格计分算法", "价格计分算法", "同时中标最多数量",
                 "同时中标最多数量"],
                ["序号", "工程名称", "分标 名称", "权重设置 技:商:价", "价格公式",
                 "下浮系数", "正向系数", "负向系数", "总包数", "限包数"],
                ["1", "陕西-河南工程", "换流站土建施工", "35:25:40", "公式2",
                 "0.95", "1.05", "0.97", "2", "1"],
            ])
        store = EvidenceStore(doc_id="t")
        counters = {"technical": 0, "commercial": 0, "price": 0}
        items: list = []
        _parse_price_matrix(tb, store, counters, items)
        assert len(items) == 1
        it = items[0]
        assert it.category == "price" and it.max_score == 40
        assert it.parsed_rule["lot"] == "换流站土建施工"
        assert it.parsed_rule["下浮系数"] == 0.95
        assert it.parsed_rule["正向系数"] == 1.05
        assert "公式2" in (it.formula or "")
