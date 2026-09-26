"""MCP (Model Context Protocol) Streamable-HTTP 端点——供 Yuxi 等 Agent 平台集成。

Yuxi 的 MCP 仅支持 sse / streamable_http 两种远程传输（langchain-mcp-adapters 客户端），
本端点实现其必需的 JSON-RPC 方法：
  initialize / notifications/initialized / tools/list / tools/call / ping

暴露工具：
  analyze_tender(file_path | file_url | file_base64, use_llm)  解析招标文件 → 结构化报告摘要
  get_report(doc_id)                                           取完整解析报告

无状态实现：不校验会话、GET 返回 405（规范允许无 SSE 通知流）。
"""
from __future__ import annotations

import base64
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "bidmaster", "version": "0.1.0"}

router = APIRouter()

TOOLS_SCHEMA: list[dict] = [
    {
        "name": "analyze_tender",
        "description": (
            "解析招标文件（.docx/.pdf/.xlsx 或整包 .zip，自动递归解压），返回结构化报告："
            "关键字段（项目名称/编号/限价/工期/保证金等，带原文证据页码）、评分项"
            "（技术/商务/价格分类、分值、评分细则、证明材料）、四类技术要求条目"
            "（类似业绩/项目负责人/人员配备/工作方案）、商务六类、★实质性条款、分值校验。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "local_path": {"type": "string",
                               "description": "服务器本地文件路径（与 file_url/file_base64 三选一）"},
                "file_url": {"type": "string", "description": "可下载的文件 URL"},
                "file_base64": {"type": "string", "description": "文件内容的 base64"},
                "file_name": {"type": "string", "description": "文件名（base64 时用于识别类型）"},
                "use_llm": {"type": "boolean", "description": "是否启用 LLM 兜底（默认 true，未配置 Key 自动跳过）"},
                "force": {"type": "boolean", "description": "强制重跑（忽略缓存；解析逻辑升级后旧缓存会自动失效，一般无需指定）"},
            },
        },
    },
    {
        "name": "get_report",
        "description": "按 doc_id 获取完整招标文件解析报告（JSON）。",
        "inputSchema": {
            "type": "object",
            "properties": {"doc_id": {"type": "string"}},
            "required": ["doc_id"],
        },
    },
]


def _resolve_local_path(p: str) -> Path:
    """local_path 白名单校验：只允许工作目录与 BIDMASTER_ALLOWED_PATHS 配置的目录。

    防任意文件读：拒绝 `..` 逃逸与符号链接逃逸（resolve 后必须落在白名单目录内）。
    """
    from bidmaster.config import get_settings
    rp = Path(p).resolve()
    allowed = [Path.cwd().resolve()]
    allowed += [Path(x).resolve() for x in get_settings().allowed_paths]
    if not any(rp.is_relative_to(a) for a in allowed):
        raise ValueError(
            f"路径不在白名单内: {p}（允许：工作目录及 BIDMASTER_ALLOWED_PATHS 配置的目录）")
    return rp


def _tool_analyze_tender(args: dict) -> str:
    from bidmaster.orchestration.pipeline import Pipeline

    local_path = args.get("local_path")
    tmpdir = Path(tempfile.mkdtemp(prefix="mcp_upload_"))
    if not local_path:
        file_name = args.get("file_name") or "tender.docx"
        suffix = Path(file_name).suffix.lower() or ".docx"
        target = tmpdir / f"input{suffix}"
        if args.get("file_base64"):
            target.write_bytes(base64.b64decode(args["file_base64"]))
        elif args.get("file_url"):
            import httpx
            resp = httpx.get(args["file_url"], timeout=120, follow_redirects=True)
            resp.raise_for_status()
            target.write_bytes(resp.content)
        else:
            raise ValueError("需要 local_path / file_url / file_base64 之一")
        local_path = str(target)
    else:
        local_path = str(_resolve_local_path(local_path))

    report = Pipeline(work_root=Path("work")).run(
        local_path, use_llm=bool(args.get("use_llm", True)),
        force=bool(args.get("force", False)))
    # 管线内存态返回 pydantic 对象（磁盘缓存态为 dict）——统一序列化
    rd = report.model_dump(mode="json")
    fields = {}
    for k, v in (rd.get("fields") or {}).items():
        fields[k] = {"value": v.get("value_normalized"), "status": v.get("status"),
                     "confidence": v.get("confidence")}
    summary = {
        "doc_id": report.doc_id,
        "file_name": report.file_name,
        "stats": rd.get("stats", {}),
        "fields": fields,
        "score_items": [{"score_id": s["score_id"], "category": s["category"],
                         "name": s["name"], "max_score": s["max_score"],
                         "lot": (s.get("parsed_rule") or {}).get("lot", "")}
                        for s in (rd.get("scores") or [])],
        "requirements_count": len(rd.get("requirements") or []),
        "rejection_risks": [{"category": r.get("category"), "text": r.get("text", "")[:80]}
                            for r in (rd.get("rejections") or [])[:8]],
        "star_clauses": [st.get("text", "")[:60] for st in (rd.get("star_clauses") or [])][:5],
        "issues": rd.get("issues", []),
        "hint": f"完整报告请用 get_report(doc_id='{report.doc_id}') 获取",
    }
    import json
    return json.dumps(summary, ensure_ascii=False, indent=2)


def _tool_get_report(args: dict) -> str:
    import json
    from bidmaster.orchestration.pipeline import Pipeline
    report = Pipeline(work_root=Path("work")).load_report(args["doc_id"])
    if report is None:
        raise ValueError(f"报告不存在: {args['doc_id']}")
    return json.dumps(report.model_dump(mode="json"), ensure_ascii=False)


_HANDLERS = {"analyze_tender": _tool_analyze_tender, "get_report": _tool_get_report}


def _rpc_result(rpc_id: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": rpc_id, "result": result}


def _rpc_error(rpc_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": rpc_id,
            "error": {"code": code, "message": message}}


def _handle_message(body: dict) -> dict | None:
    method = body.get("method", "")
    rpc_id = body.get("id")
    params = body.get("params") or {}

    if method == "initialize":
        return _rpc_result(rpc_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": SERVER_INFO,
        })
    if method.startswith("notifications/"):  # initialized 等：通知无需响应
        return None
    if method == "ping":
        return _rpc_result(rpc_id, {})
    if method == "tools/list":
        return _rpc_result(rpc_id, {"tools": TOOLS_SCHEMA})
    if method == "tools/call":
        name = params.get("name", "")
        args = params.get("arguments") or {}
        handler = _HANDLERS.get(name)
        if handler is None:
            return _rpc_error(rpc_id, -32602, f"未知工具: {name}")
        try:
            # 解析是 CPU 密集操作，放线程避免阻塞事件循环
            result_box: dict = {}
            err_box: dict = {}

            def _run():
                try:
                    result_box["text"] = handler(args)
                except Exception as e:  # noqa: BLE001
                    err_box["e"] = e

            t = threading.Thread(target=_run, daemon=True)
            t.start()
            t.join(timeout=900)
            if "e" in err_box:
                return _rpc_result(rpc_id, {
                    "content": [{"type": "text", "text": f"执行失败: {err_box['e']}"}],
                    "isError": True})
            if "text" not in result_box:
                return _rpc_result(rpc_id, {
                    "content": [{"type": "text", "text": "执行超时"}], "isError": True})
            return _rpc_result(rpc_id, {
                "content": [{"type": "text", "text": result_box["text"]}],
                "isError": False})
        except Exception as e:  # noqa: BLE001
            return _rpc_error(rpc_id, -32603, f"内部错误: {e}")
    return _rpc_error(rpc_id, -32601, f"Method not found: {method}")


@router.post("/mcp")
async def mcp_endpoint(request: Request):
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse(_rpc_error(None, -32700, "Parse error"), status_code=400)
    result = _handle_message(body)
    if result is None:  # 通知
        return Response(status_code=202)
    return JSONResponse(result)


@router.get("/mcp")
async def mcp_get():
    # 无服务器→客户端 SSE 通知流（规范允许拒绝）
    return Response(status_code=405)
