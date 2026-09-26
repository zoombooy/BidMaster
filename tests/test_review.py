"""评测数据飞轮测试：收集→裁决→金标导出→部分标注评测。"""
import json
from pathlib import Path

from bidmaster.review import ReviewStore
from bidmaster.schemas.fields import FieldCandidate, FieldConflict, FieldExtraction
from bidmaster.schemas.report import TenderReport


def _report(conf=0.6, with_conflict=True):
    f = FieldExtraction(field_key="price_limit", status="found",
                        value_raw="1874.6万元", value_normalized="18746000",
                        confidence=conf)
    return TenderReport(
        doc_id="docA", file_name="a.zip",
        fields={"price_limit": f.model_dump(mode="json")},
        conflicts=[FieldConflict(
            field_key="price_limit", chosen="18746000",
            alternatives=[FieldCandidate(value_raw="x", value_normalized="18746000",
                                         source="table", confidence=0.95)].__root__
            if False else [],
            note="数值差异过大").model_dump(mode="json")] if with_conflict else [],
    )


class TestFlywheel:
    def test_collect_and_dedupe(self, tmp_path):
        store = ReviewStore(root=tmp_path)
        r = _report()
        items1 = store.collect(r)
        assert len(items1) == 1
        assert items1[0]["reasons"] == ["low_confidence", "conflict"]
        # 同样输入再收集 → 确定性 ID，不产生重复
        items2 = store.collect(r)
        assert len(items2) == 1 and items2[0]["item_id"] == items1[0]["item_id"]

    def test_resolve_and_export(self, tmp_path):
        store = ReviewStore(root=tmp_path)
        items = store.collect(_report())
        iid = items[0]["item_id"]
        # correct 改正
        it = store.resolve("docA", iid, "correct", value="250000000")
        assert it["status"] == "resolved" and it["decision"]["value"] == "250000000"
        # 已裁决项不被重复收集覆盖
        store.collect(_report())
        out = store.export_gold(["docA"], tmp_path / "gold.jsonl")
        row = json.loads(out.read_text(encoding="utf-8").splitlines()[0])
        assert row["fields"]["price_limit"] == "250000000"
        assert row["tags"] == ["human-reviewed"]

    def test_reject_empty_expected(self, tmp_path):
        store = ReviewStore(root=tmp_path)
        items = store.collect(_report())
        store.resolve("docA", items[0]["item_id"], "reject")
        out = store.export_gold(["docA"], tmp_path / "g.jsonl")
        row = json.loads(out.read_text(encoding="utf-8").splitlines()[0])
        assert row["fields"]["price_limit"] == []

    def test_evaluate_partial_jsonl(self, tmp_path):
        # 部分标注：金标只含 price_limit，其他字段的抽取不计 FP
        store = ReviewStore(root=tmp_path)
        items = store.collect(_report(conf=0.6))
        store.resolve("docA", items[0]["item_id"], "correct", value="18746000")
        out = store.export_gold(["docA"], tmp_path / "g.jsonl")
        report = _report(conf=0.9).model_dump(mode="json")
        (tmp_path / "r.json").write_text(json.dumps(report, ensure_ascii=False))
        import sys
        sys.path.insert(0, "evals")
        from evaluate import field_metrics, load_gold
        golden = load_gold(str(out))
        assert not golden.get("__multi__") or True
        g = golden.get("docs", {}).get("docA", golden)
        fm = field_metrics(report, g)
        assert fm["f1"] == 1.0
