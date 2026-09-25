"""处理账本：保证"无静默遗漏"——每个页面对象必须落入三种终态之一，
流程结束有完成门校验（borrowed from bid-agent-vscode 的方法论）。

终态：
  DONE               已处理
  EXCLUDED_WITH_REASON 明确排除并记录原因（如目录页、空白页）
  FAILED_REVIEW      失败转人工（如 OCR 不可用、加密文件）
未落账本的对象视为未处理，完成门不通过。
"""
from __future__ import annotations

from pydantic import BaseModel, Field

STATUS_PENDING = "PENDING"
STATUS_DONE = "DONE"
STATUS_EXCLUDED = "EXCLUDED_WITH_REASON"
STATUS_FAILED = "FAILED_REVIEW"


class LedgerItem(BaseModel):
    key: str                      # 如 page:12 / table:t-0007 / file:xxx.docx
    kind: str                     # page | table | file | attachment
    status: str = STATUS_PENDING
    reason: str = ""              # EXCLUDED / FAILED 时必填


class ProcessingLedger(BaseModel):
    doc_id: str = ""
    items: dict[str, LedgerItem] = Field(default_factory=dict)

    def register(self, key: str, kind: str) -> LedgerItem:
        if key not in self.items:
            self.items[key] = LedgerItem(key=key, kind=kind)
        return self.items[key]

    def done(self, key: str) -> None:
        self._mark(key, STATUS_DONE)

    def exclude(self, key: str, reason: str) -> None:
        self._mark(key, STATUS_EXCLUDED, reason)

    def fail(self, key: str, reason: str) -> None:
        self._mark(key, STATUS_FAILED, reason)

    def _mark(self, key: str, status: str, reason: str = "") -> None:
        it = self.items.get(key) or self.register(key, key.split(":", 1)[0])
        it.status = status
        if reason:
            it.reason = reason

    def completion_gate(self) -> tuple[bool, list[str]]:
        """完成门：存在 PENDING 对象则不允许任务标记完成。"""
        pending = [k for k, v in self.items.items() if v.status == STATUS_PENDING]
        return (len(pending) == 0, pending)

    def summary(self) -> dict:
        done = sum(1 for v in self.items.values() if v.status == STATUS_DONE)
        excl = sum(1 for v in self.items.values() if v.status == STATUS_EXCLUDED)
        fail = sum(1 for v in self.items.values() if v.status == STATUS_FAILED)
        ok, pending = self.completion_gate()
        return {
            "total": len(self.items), "done": done, "excluded": excl,
            "failed_review": fail, "pending": len(pending),
            "complete_gate_passed": ok,
        }
