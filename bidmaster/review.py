"""评测数据飞轮：复核队列 → 人工裁决 → 金标回流（对标 tender-extract review.py 设计）。

闭环：
  报告产出 → 低置信(<0.7)/有冲突 的字段自动入队（确定性 ID，重复抽取不产生重复待办）
  → `bidmaster review resolve` 人工裁决（accept 采纳 / correct 改正 / reject 判不应抽取）
  → `bidmaster review export` 把 resolved 决策导出为部分标注金标 JSONL
  → `evals/evaluate.py` 只评标注字段，未标注字段不计 FP → CI 门禁

reject 的语义：该字段本不应被抽取 → 导出 expected 为空列表，
后续抽取若再次命中即在评测中显形为 FP 回归，而不是被静默忽略。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from bidmaster.schemas.report import TenderReport

REVIEW_THRESHOLD = 0.7


def _item_id(doc_id: str, field_key: str, candidates: list[str]) -> str:
    payload = json.dumps([doc_id, field_key, sorted(candidates)],
                         ensure_ascii=False, sort_keys=True)
    return "rv-" + hashlib.sha256(payload.encode()).hexdigest()[:20]


class ReviewStore:
    def __init__(self, root: Path | str = "work"):
        self.root = Path(root)

    def _store_path(self, doc_id: str) -> Path:
        return self.root / doc_id / "review.jsonl"

    # ---------- 收集 ----------
    def collect(self, report: TenderReport) -> list[dict]:
        """从报告收集复核项（低置信 + 有冲突），upsert 语义：已裁决项不覆盖。"""
        path = self._store_path(report.doc_id)
        existing: dict[str, dict] = {}
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    it = json.loads(line)
                    existing[it["item_id"]] = it

        report_issues_low: set = set()  # 预留：issues 汇总文本的精细匹配
        del report_issues_low

        for key, f in (report.fields or {}).items():
            status = f.get("status") if isinstance(f, dict) else f.status
            conf = f.get("confidence") if isinstance(f, dict) else f.confidence
            if status != "found":
                continue
            reasons: list[str] = []
            if float(conf or 0) < REVIEW_THRESHOLD:
                reasons.append("low_confidence")
            conflicts = {c.field_key: c for c in (report.conflicts or [])}
            if key in conflicts:
                reasons.append("conflict")
            if not reasons:
                continue
            cands = f.get("candidates") if isinstance(f, dict) else getattr(f, "candidates", [])
            cand_values = sorted({str(c.get("value_normalized") or "")
                                  for c in (cands or [])})
            iid = _item_id(report.doc_id, key, cand_values)
            if iid in existing and existing[iid].get("status") == "resolved":
                continue  # 已裁决项永不被覆盖
            primary = f.get("value_normalized") if isinstance(f, dict) else None
            existing[iid] = {
                "item_id": iid, "doc_id": report.doc_id, "field_key": key,
                "primary_value": primary, "confidence": conf,
                "reasons": reasons,
                "candidates": [{"value": c.get("value_normalized"),
                                "source": c.get("source"),
                                "confidence": c.get("confidence")}
                               for c in (cands or [])][:6],
                "status": "open",
                "decision": None,
            }
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text("\n".join(json.dumps(it, ensure_ascii=False)
                                 for it in existing.values()), encoding="utf-8")
        tmp.replace(path)
        return [it for it in existing.values() if it["status"] == "open"]

    # ---------- 裁决 ----------
    def resolve(self, doc_id: str, item_id: str, action: str,
                value: str | None = None) -> dict:
        """action: accept（采纳 primary）/ correct（必须给修正值）/ reject（判不应抽取）。"""
        path = self._store_path(doc_id)
        items: dict[str, dict] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                it = json.loads(line)
                items[it["item_id"]] = it
        it = items.get(item_id)
        if it is None:
            raise ValueError(f"复核项不存在: {item_id}")
        if action == "accept":
            it["decision"] = {"action": "accept",
                              "value": it.get("primary_value")}
        elif action == "correct":
            if not value:
                raise ValueError("correct 必须提供修正值 --value")
            it["decision"] = {"action": "correct", "value": value}
        elif action == "reject":
            it["decision"] = {"action": "reject", "value": ""}
        else:
            raise ValueError(f"未知 action: {action}")
        it["status"] = "resolved"
        tmp = path.with_suffix(".tmp")
        tmp.write_text("\n".join(json.dumps(x, ensure_ascii=False)
                                 for x in items.values()), encoding="utf-8")
        tmp.replace(path)
        return it

    # ---------- 金标导出 ----------
    def export_gold(self, doc_ids: list[str], out_path: Path) -> Path:
        """把 resolved 决策聚合为部分标注金标 JSONL（每文档一行）。"""
        rows: dict[str, dict] = {}
        for doc_id in doc_ids:
            path = self._store_path(doc_id)
            if not path.exists():
                continue
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                it = json.loads(line)
                if it.get("status") != "resolved" or not it.get("decision"):
                    continue
                row = rows.setdefault(doc_id, {
                    "id": "review-" + hashlib.sha256(doc_id.encode()).hexdigest()[:12],
                    "document": doc_id,
                    "fields": {},
                    "tags": ["human-reviewed"],
                })
                if it["decision"]["action"] == "reject":
                    row["fields"][it["field_key"]] = []  # 空期望：再命中即 FP
                else:
                    row["fields"][it["field_key"]] = it["decision"]["value"]
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text("\n".join(json.dumps(r, ensure_ascii=False)
                                      for r in rows.values()), encoding="utf-8")
        return out_path
