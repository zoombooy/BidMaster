"""A2A 任务执行服务：把收到的招标文件交给一期管线，异步推进任务状态。"""
from __future__ import annotations

import base64
import threading
import uuid
from pathlib import Path

import httpx

from .models import Artifact, Message, Part, TaskState
from .store import store

ALLOWED_MIME = {
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/msword": ".doc",
}
ALLOWED_EXT = {".pdf", ".docx", ".doc"}


def extract_input_file(parts: list[Part]) -> tuple[Path | None, str | None]:
    """从 Message parts 提取招标文件 → (本地文件路径, 错误)。

    支持：FilePart（base64 bytes 或 uri）、DataPart（{file_base64, file_name}）。
    """
    import tempfile
    tmpdir = Path(tempfile.mkdtemp(prefix="bidmaster_a2a_"))
    for part in parts:
        if part.kind == "file" and part.file:
            mime = part.file.mimeType or ""
            name = part.file.name or "tender"
            suffix = Path(name).suffix.lower() or ALLOWED_MIME.get(mime, "")
            if not suffix:
                suffix = ".pdf"
            if suffix not in ALLOWED_EXT:
                return None, f"不支持的文件类型 {suffix}（支持 pdf/docx/doc）"
            path = tmpdir / f"input{suffix}"
            if part.file.bytes:
                try:
                    path.write_bytes(base64.b64decode(part.file.bytes))
                except Exception:  # noqa: BLE001
                    return None, "file.bytes 不是合法的 base64"
                return path, None
            if part.file.uri:
                try:
                    resp = httpx.get(part.file.uri, timeout=60, follow_redirects=True)
                    resp.raise_for_status()
                    path.write_bytes(resp.content)
                except Exception as e:  # noqa: BLE001
                    return None, f"下载 file.uri 失败: {e}"
                return path, None
        if part.kind == "data" and part.data and part.data.get("file_base64"):
            name = part.data.get("file_name", "tender.docx")
            suffix = Path(name).suffix.lower() or ".docx"
            path = tmpdir / f"input{suffix}"
            try:
                path.write_bytes(base64.b64decode(part.data["file_base64"]))
            except Exception:  # noqa: BLE001
                return None, "data.file_base64 不是合法的 base64"
            return path, None
    return None, ("消息中未找到招标文件：请携带 FilePart（base64 或 uri，"
                  "application/pdf 或 docx）或 DataPart{file_base64, file_name}")


def submit_task(message: Message, metadata: dict | None = None) -> dict:
    """message/send 入口：建任务 → 后台线程跑管线 → 立即返回 Task。"""
    task = store.create(message, metadata=metadata)
    thread = threading.Thread(target=_run_task, args=(task.id, message), daemon=True)
    thread.start()
    return store.get(task.id).model_dump(mode="json")  # type: ignore[union-attr]


def _run_task(task_id: str, message: Message) -> None:
    store.update_state(task_id, TaskState.WORKING)
    try:
        path, err = extract_input_file(message.parts)
        if err:
            _fail(task_id, err)
            return
        from bidmaster.orchestration.pipeline import Pipeline
        report = Pipeline(work_root=Path("work")).run(str(path), use_llm=True)
        artifact = Artifact(
            artifactId=uuid.uuid4().hex,
            name="tender_report.json",
            description="招标文件结构化解析报告（字段/评分项/要求条目/证据链）",
            parts=[Part(kind="data", data=report.model_dump(mode="json"))],
            index=0, lastChunk=True,
        )
        store.add_artifact(task_id, artifact)
        store.update_state(task_id, TaskState.COMPLETED, message=Message(
            role="agent",
            parts=[Part(kind="text", text=(
                f"解析完成：字段 {report.stats['fields_found']}/{report.stats['fields_total']}，"
                f"评分项 {report.stats['score_items']}，要求条目 "
                f"{report.stats['requirements']}，★条款 {report.stats['star_clauses']}"))],
        ))
        _notify(task_id)
    except Exception as e:  # noqa: BLE001
        _fail(task_id, f"解析失败: {e}")


def _fail(task_id: str, reason: str) -> None:
    store.update_state(task_id, TaskState.FAILED, message=Message(
        role="agent", parts=[Part(kind="text", text=reason)]))
    _notify(task_id)


def _notify(task_id: str) -> None:
    """任务终态推送（若配置了 pushNotificationConfig）。best-effort。"""
    cfg = store.get_push_config(task_id)
    task = store.get(task_id)
    if cfg is None or task is None:
        return
    headers = {"X-A2A-Notification-Token": cfg.token} if cfg.token else {}
    try:
        httpx.post(cfg.url, json=task.model_dump(mode="json"),
                   headers=headers, timeout=15)
    except Exception:  # noqa: BLE001 推送失败不影响任务结果
        pass
