"""A2A 任务存储：内存为主 + JSON 落盘（work/a2a_tasks/），带状态事件流（供 SSE 重放）。"""
from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .models import Artifact, Message, PushNotificationConfig, Task, TaskState, TaskStatus


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class A2ATaskStore:
    def __init__(self, root: Path | str = "work/a2a_tasks"):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._tasks: dict[str, Task] = {}
        self._events: dict[str, list[dict]] = {}   # task_id -> [{"kind": ..., ...}]
        self._push: dict[str, PushNotificationConfig] = {}
        self._load()

    # ---------- 持久化 ----------
    def _load(self) -> None:
        for f in sorted(self.root.glob("task_*.json")):
            try:
                import json
                data = json.loads(f.read_text(encoding="utf-8"))
                self._tasks[data["id"]] = Task.model_validate(data)
            except Exception:  # noqa: BLE001 损坏文件跳过
                continue

    def _persist(self, task: Task) -> None:
        import json
        (self.root / f"task_{task.id}.json").write_text(
            json.dumps(task.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8")

    # ---------- 任务生命周期 ----------
    def create(self, message: Message, metadata: dict | None = None) -> Task:
        task_id = uuid.uuid4().hex
        task = Task(
            id=task_id,
            contextId=message.contextId or uuid.uuid4().hex,
            status=TaskStatus(state=TaskState.SUBMITTED, timestamp=_now()),
            history=[message],
            metadata=metadata,
        )
        with self._lock:
            self._tasks[task_id] = task
            self._events[task_id] = [{"kind": "task", "task": task.model_dump(mode="json")}]
            self._persist(task)
        return task

    def get(self, task_id: str) -> Task | None:
        with self._lock:
            return self._tasks.get(task_id)

    def update_state(self, task_id: str, state: TaskState,
                     message: Message | None = None) -> Task | None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return None
            task.status = TaskStatus(state=state, message=message, timestamp=_now())
            self._events[task_id].append({
                "kind": "status-update", "taskId": task_id,
                "contextId": task.contextId,
                "status": task.status.model_dump(mode="json"),
                "final": state in (TaskState.COMPLETED, TaskState.FAILED,
                                   TaskState.CANCELED, TaskState.REJECTED),
            })
            self._persist(task)
            return task

    def add_artifact(self, task_id: str, artifact: Artifact) -> Task | None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return None
            task.artifacts.append(artifact)
            self._events[task_id].append({
                "kind": "artifact-update", "taskId": task_id,
                "contextId": task.contextId,
                "artifact": artifact.model_dump(mode="json"),
            })
            self._persist(task)
            return task

    def cancel(self, task_id: str) -> tuple[Task | None, str | None]:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return None, "Task not found"
            if task.status.state not in (TaskState.SUBMITTED, TaskState.WORKING):
                return task, "Task is not in a cancelable state"
            task.status = TaskStatus(state=TaskState.CANCELED, timestamp=_now())
            self._events[task_id].append({
                "kind": "status-update", "taskId": task_id,
                "contextId": task.contextId,
                "status": task.status.model_dump(mode="json"), "final": True})
            self._persist(task)
            return task, None

    def events(self, task_id: str) -> list[dict]:
        with self._lock:
            return list(self._events.get(task_id, []))

    # ---------- 推送通知 ----------
    def set_push_config(self, task_id: str, cfg: PushNotificationConfig) -> bool:
        with self._lock:
            if task_id not in self._tasks:
                return False
            self._push[task_id] = cfg
            return True

    def get_push_config(self, task_id: str) -> PushNotificationConfig | None:
        with self._lock:
            return self._push.get(task_id)


# 全局单例（FastAPI 进程内共享）
store = A2ATaskStore()
