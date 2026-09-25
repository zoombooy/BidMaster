"""A2A 协议 HTTP 层：
  GET  /.well-known/agent.json（及 agent-card.json）—— 能力发现
  POST /a2a  —— JSON-RPC 2.0：message/send / message/stream(SSE) / tasks/get
                / tasks/cancel / tasks/pushNotificationConfig.set|get
"""
from __future__ import annotations

import asyncio
import json
import threading

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .models import (A2AError, AgentCapabilities, AgentCard, AgentProvider,
                     AgentSkill, Message, PushNotificationConfig, Task,
                     TaskPushNotificationConfig, TaskState, PROTOCOL_VERSION,
                     rpc_error, rpc_result)
from .service import submit_task
from .store import store

router = APIRouter()

_INPUT_MODES = [
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/msword",
]
_OUTPUT_MODES = ["application/json", "text/plain"]


def build_agent_card(base_url: str) -> dict:
    card = AgentCard(
        name="BidMaster 招标文件解析 Agent",
        description=(
            "把招标文件（PDF/Word/扫描件）变成结构化、可溯源的数据：关键字段抽取"
            "（项目名称/编号/限价/工期/保证金等）、技术评分标准结构化（类似业绩/"
            "项目负责人/人员配备/工作方案）、商务评分标准结构化、★实质性条款、"
            "全文证据链。返回 JSON 报告，可直接驱动标书生成或合规检查。"),
        url=f"{base_url.rstrip('/')}/a2a",
        provider=AgentProvider(organization="zoombooy",
                               url="https://github.com/zoombooy/BidMaster"),
        version="0.1.0",
        protocolVersion=PROTOCOL_VERSION,
        capabilities=AgentCapabilities(streaming=True, pushNotifications=True),
        defaultInputModes=_INPUT_MODES,
        defaultOutputModes=_OUTPUT_MODES,
        skills=[AgentSkill(
            id="tender_analysis",
            name="招标文件解析与评分标准结构化",
            description=(
                "输入招标文件，输出结构化解析报告：17 个关键字段（带证据页码/坐标/"
                "原文片段）、评分项（技术/商务/价格）、四类技术要求条目、商务六类、"
                "★实质性条款、分值合计校验。"),
            tags=["招标", "标书", "tender", "bid", "document-parsing", "scoring"],
            examples=[
                "解析这份招标文件，提取限价、工期和评分标准",
                "这份文件的技术标评分项有哪些？每项多少分？",
                "提取项目负责人的资格要求和★实质性条款",
            ],
            inputModes=_INPUT_MODES,
            outputModes=_OUTPUT_MODES,
        )],
    )
    return card.model_dump(mode="json")


@router.get("/.well-known/agent.json")
@router.get("/.well-known/agent-card.json")
def agent_card(request: Request):
    base = str(request.base_url).rstrip("/")
    # 反向代理场景取 Host 头
    host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    if host:
        scheme = request.headers.get("x-forwarded-proto",
                                     request.url.scheme)
        base = f"{scheme}://{host}"
    return build_agent_card(base)


# ---------------- JSON-RPC ----------------
def _task_from_params(params: dict) -> tuple[str | None, dict | None]:
    task_id = params.get("id")
    if not task_id:
        return None, rpc_error(A2AError.TASK_NOT_FOUND, "缺少任务 id")
    return task_id, None


async def _handle_rpc(method: str, params: dict, rpc_id):
    if method == "message/send":
        msg = params.get("message")
        if not msg:
            return rpc_error(-32602, "缺少 message 参数", rpc_id)
        try:
            message = Message.model_validate(msg)
        except Exception as e:  # noqa: BLE001
            return rpc_error(-32602, f"message 格式非法: {e}", rpc_id)
        task = submit_task(message, metadata=params.get("metadata"))
        return rpc_result(task, rpc_id)

    if method == "tasks/get":
        task_id, err = _task_from_params(params)
        if err:
            return {**err, "id": rpc_id}
        task = store.get(task_id)
        if task is None:
            return rpc_error(A2AError.TASK_NOT_FOUND, f"Task not found: {task_id}", rpc_id)
        return rpc_result(task.model_dump(mode="json"), rpc_id)

    if method == "tasks/cancel":
        task_id, err = _task_from_params(params)
        if err:
            return {**err, "id": rpc_id}
        task, error = store.cancel(task_id)
        if task is None:
            return rpc_error(A2AError.TASK_NOT_FOUND, f"Task not found: {task_id}", rpc_id)
        if error:
            return rpc_error(A2AError.TASK_NOT_CANCELABLE, error, rpc_id)
        return rpc_result(task.model_dump(mode="json"), rpc_id)

    if method in ("tasks/pushNotificationConfig/set", "tasks/pushNotificationConfig/get"):
        if method.endswith("/get"):
            cfg = store.get_push_config(params.get("taskId", ""))
            if cfg is None:
                return rpc_error(A2AError.TASK_NOT_FOUND, "未配置推送", rpc_id)
            return rpc_result(TaskPushNotificationConfig(
                taskId=params["taskId"], pushNotificationConfig=cfg
            ).model_dump(mode="json"), rpc_id)
        try:
            payload = TaskPushNotificationConfig.model_validate(params)
        except Exception as e:  # noqa: BLE001
            return rpc_error(-32602, f"参数非法: {e}", rpc_id)
        ok = store.set_push_config(payload.taskId, payload.pushNotificationConfig)
        if not ok:
            return rpc_error(A2AError.TASK_NOT_FOUND, "Task not found", rpc_id)
        return rpc_result(payload.model_dump(mode="json"), rpc_id)

    return rpc_error(-32601, f"Method not found: {method}", rpc_id)


@router.post("/a2a")
async def a2a_endpoint(request: Request):
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse(rpc_error(-32700, "Parse error"), status_code=400)
    method = body.get("method", "")
    rpc_id = body.get("id")
    params = body.get("params") or {}

    if method == "message/stream":
        return await _message_stream(params, rpc_id)

    result = await _handle_rpc(method, params, rpc_id)
    status = 400 if "error" in result else 200
    return JSONResponse(result, status_code=status)


# ---------------- SSE 流式 ----------------
async def _message_stream(params: dict, rpc_id):
    msg = params.get("message")
    if not msg:
        return JSONResponse(rpc_error(-32602, "缺少 message 参数", rpc_id), status_code=400)
    try:
        message = Message.model_validate(msg)
    except Exception as e:  # noqa: BLE001
        return JSONResponse(rpc_error(-32602, f"message 格式非法: {e}", rpc_id),
                            status_code=400)

    task = store.create(message, metadata=params.get("metadata"))
    task_id = task.id
    threading.Thread(target=_run_task, args=(task_id, message),
                     daemon=True).start()

    async def event_source():
        # 首个事件：任务已受理
        first = store.get(task_id)
        yield _sse(rpc_result(first.model_dump(mode="json"), rpc_id))
        sent_idx = 1  # 跳过首个 "task" 事件（已随受理响应发送）
        final_seen = False
        while not final_seen:
            await asyncio.sleep(0.25)
            events = store.events(task_id)
            for ev in events[sent_idx:]:
                sent_idx += 1
                if ev.get("kind") in ("status-update", "artifact-update"):
                    yield _sse(rpc_result(ev, rpc_id))
                if ev.get("final"):
                    final_seen = True

    return StreamingResponse(event_source(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "Connection": "keep-alive"})


def _run_task(task_id: str, message: Message) -> None:
    from .service import _run_task
    _run_task(task_id, message)


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
