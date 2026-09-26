"""评测 harness：报告 vs 金标 → 字段级 P/R/F1 + 评分项召回率。

用法：
  python evals/evaluate.py work/<doc_id>/06_report.json evals/golden/sample_golden.json
金标格式见 evals/golden/sample_golden.json。--fail-under N 可作 CI 门禁。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def load_gold(path: str) -> dict:
    """加载金标：支持两种格式——
    1) 单文档 JSON（{"fields": {...}, "score_items": [...]}, 见 sample_golden.json）
    2) 复核导出的部分标注 JSONL（每行 {"id","document","fields":{...}}，只评标注字段）"""
    p = Path(path)
    text = p.read_text(encoding="utf-8").strip()
    if text.startswith("[") or "\n" in text and not text.startswith("{"):
        rows = [json.loads(line) for line in text.splitlines() if line.strip()]
        merged: dict[str, dict] = {}
        score_items: list[str] = []
        for row in rows:
            doc = row.get("document", row.get("id", ""))
            g = merged.setdefault(doc, {"fields": {}, "score_items": []})
            g["fields"].update(row.get("fields", {}))
            score_items.extend(row.get("score_items", []))
        # 多文档行：返回 {"docs": {doc_id: golden}} 供多报告评测
        return {"__multi__": True, "docs": merged, "score_items": score_items}
    return json.loads(text)


def field_metrics(report: dict, golden: dict) -> dict:
    """按字段统计：金标给出期望 normalized 值（可多个）。部分标注安全——只评金标里出现的字段。"""
    fields = report.get("fields", {})
    tp = fp = fn = 0
    details: dict[str, dict] = {}
    for key, expect in golden.get("fields", {}).items():
        expected = [str(v) for v in (expect if isinstance(expect, list) else [expect])]
        got = fields.get(key, {})
        actual = str(got.get("value_normalized")) if got.get("status") == "found" else None
        if actual in expected:
            tp += 1
            details[key] = {"ok": True, "actual": actual}
        elif actual is None:
            fn += 1
            details[key] = {"ok": False, "actual": None, "expected": expected}
        else:
            fp += 1
            details[key] = {"ok": False, "actual": actual, "expected": expected}
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * p * r / (p + r) if p + r else 0.0
    return {"precision": round(p, 3), "recall": round(r, 3), "f1": round(f1, 3),
            "tp": tp, "fp": fp, "fn": fn, "details": details}


def score_recall(report: dict, golden: dict) -> dict:
    """评分项召回：金标评分项名称（模糊包含匹配）。"""
    expect: list[str] = golden.get("score_items", [])
    got_names = [s.get("name", "") for s in report.get("scores", [])]
    missed = []
    for name in expect:
        if not any(name in g or g in name for g in got_names):
            missed.append(name)
    recall = (len(expect) - len(missed)) / len(expect) if expect else 1.0
    return {"expected": len(expect), "found": len(expect) - len(missed),
            "recall": round(recall, 3), "missed": missed}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("report")
    ap.add_argument("golden")
    ap.add_argument("--fail-under", type=float, default=0.0,
                    help="F1 低于阈值则退出码 1（CI 门禁）")
    args = ap.parse_args(argv)

    report = load(args.report)
    golden_raw = load(args.golden)

    if golden_raw.get("__multi__"):
        doc_id = report.get("doc_id")
        golden = golden_raw["docs"].get(doc_id, {"fields": {}, "score_items": []})
        if not golden["fields"]:
            print(f"[SKIP] 金标 JSONL 中没有文档 {doc_id} 的标注")
            return 0
    else:
        golden = golden_raw

    fm = field_metrics(report, golden)
    sr = score_recall(report, golden)
    print(json.dumps({
        "field_metrics": {k: v for k, v in fm.items() if k != "details"},
        "score_recall": sr,
        "field_details": fm["details"],
    }, ensure_ascii=False, indent=2))
    if fm["f1"] < args.fail_under:
        print(f"[FAIL] F1 {fm['f1']} < 阈值 {args.fail_under}")
        return 1
    print(f"[PASS] F1 {fm['f1']} >= 阈值 {args.fail_under}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
