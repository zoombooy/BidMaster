"""端到端管线测试：生成示例招标文件 → 完整管线 → 校验报告。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.make_sample import build_sample_docx  # noqa: E402

from bidmaster.orchestration.pipeline import Pipeline  # noqa: E402


def _run(tmp_path: Path):
    docx = build_sample_docx(tmp_path / "sample_tender.docx")
    report = Pipeline(work_root=tmp_path / "work").run(str(docx), force=True,
                                                      use_llm=False)
    return report


class TestEndToEnd:
    def test_fields(self, tmp_path):
        r = _run(tmp_path)
        f = r.fields
        # 必填核心字段
        assert f["project_name"].status == "found"
        assert "住院综合楼" in (f["project_name"].value_normalized or "")
        assert f["tender_no"].value_normalized == "XYZ-2026-GC-001"
        assert f["price_limit"].value_normalized == "250000000"
        assert f["security_deposit"].value_normalized == "800000"
        assert f["bid_deadline"].value_normalized == "2026-11-20 09:30"
        assert f["duration"].value_normalized == "540日历天"
        assert f["bid_validity"].value_normalized == "90日历天"
        assert f["evaluation_method"].status == "found"
        assert "综合评估法" in (f["evaluation_method"].value_normalized or "")
        # 证据链：每个 found 字段必须有证据
        for key, fe in f.items():
            if fe.status == "found":
                assert fe.evidence_ids, f"{key} 缺少证据"

    def test_scores_and_checks(self, tmp_path):
        r = _run(tmp_path)
        assert len(r.scores) == 10
        cats = {s.category for s in r.scores}
        assert cats == {"technical", "commercial", "price"}
        perf = next(s for s in r.scores if s.name == "类似工程业绩")
        assert perf.score_id == "T-001" and perf.max_score == 6
        # 分值合计校验通过
        assert all(c.ok for c in r.sum_checks)
        assert sum(c.computed_total for c in r.sum_checks) == 100

    def test_requirements(self, tmp_path):
        r = _run(tmp_path)
        types = {req.type for req in r.requirements}
        assert {"performance", "leader", "team"} <= types
        assert {"qualification", "finance", "credit", "honor", "price"} <= types
        # 暗标/页数/★条款
        plan = next(req for req in r.requirements if req.type == "plan")
        assert plan.parsed.get("page_limit") == 120
        assert plan.parsed.get("dark_bid")
        assert len(r.star_clauses) == 2

    def test_evidence_and_ledger(self, tmp_path):
        r = _run(tmp_path)
        assert len(r.evidence) >= 15
        ev_ids = {e.evidence_id for e in r.evidence}
        for s in r.scores:
            for eid in s.evidence_ids:
                assert eid in ev_ids
        assert r.ledger.total > 0
        assert r.stats["score_items"] == 10

    def test_resume_from_cache(self, tmp_path):
        docx = build_sample_docx(tmp_path / "sample2.docx")
        p = Pipeline(work_root=tmp_path / "work")
        p.run(str(docx), force=True, use_llm=False)
        # 第二次跑走缓存（同内容同 SHA → 同工作区）
        p2 = Pipeline(work_root=tmp_path / "work")
        r2 = p2.run(str(docx), force=False, use_llm=False)
        assert r2.stats["score_items"] == 10
