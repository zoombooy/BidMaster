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
            "（类似业绩/项目负责人/人员配备/工作方案）、商务六类、★实质性条款与废标风险。"
            "与 Yuxi 沙盒共享文件系统：用户在界面上传的文件，直接把沙盒内路径"
            "（/home/gem/user-data/...）作为 local_path 传入即可，不要用 shell 自行解压。"),
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
        "name": "list_lots",
        "description": (
            "列出招标文件的分标/标段清单（名称 + 各分标评分项数量）。"
            "多分标招标建议先调用本工具，再用 get_report 的 lot 参数按分标查看，"
            "避免不同分标的评分项混在一起。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "local_path": {"type": "string", "description": "招标文件路径（未解析过时必填）"},
                "doc_id": {"type": "string", "description": "已解析报告的 doc_id（与 local_path 二选一）"},
            },
        },
    },
    {
        "name": "get_report",
        "description": (
            "按 doc_id 获取解析报告，支持过滤与分页：lot（按分标）、category"
            "（technical/commercial/price）、section（fields/scores/requirements/"
            "rejections/sum_checks/all）、page+page_size 分页。大报告务必用过滤，"
            "避免一次取全量。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "doc_id": {"type": "string"},
                "lot": {"type": "string", "description": "按分标/标段名过滤"},
                "category": {"type": "string",
                             "description": "technical | commercial | price"},
                "section": {"type": "string",
                            "description": "fields | scores | requirements | rejections | sum_checks | all（默认 summary：字段+统计+分值校验）"},
                "page": {"type": "integer", "description": "页码，从 1 起"},
                "page_size": {"type": "integer", "description": "每页条数，默认 50，上限 200"},
            },
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

    # 目录 → 收集其中全部可解析文档，批量解析（一次传多个附件的场景）
    run_paths: list[str] | str = local_path
    if Path(local_path).is_dir():
        from bidmaster.ingestion.archive import collect_documents
        docs = collect_documents([Path(local_path)])
        if not docs:
            raise ValueError(f"目录中无可解析文档（支持 docx/pdf/xlsx/zip）: {local_path}")
        run_paths = [str(p) for p in docs]

    report = Pipeline(work_root=Path("work")).run(
        run_paths, use_llm=bool(args.get("use_llm", True)),
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
        "hint": (f"完整报告请用 get_report(doc_id='{report.doc_id}')；"
                 f"多分标文件建议先用 list_lots 查看分标清单"),
    }
    import json
    return json.dumps(summary, ensure_ascii=False, indent=2)


def _tool_list_lots(args: dict) -> str:
    import json
    from bidmaster.orchestration.pipeline import Pipeline
    pipeline = Pipeline(work_root=Path("work"))
    doc_id = args.get("doc_id")
    if not doc_id:
        report = pipeline.run(args["local_path"], use_llm=bool(args.get("use_llm", True)))
        doc_id = report.doc_id
    report = pipeline.load_report(doc_id)
    if report is None:
        raise ValueError(f"报告不存在: {doc_id}")
    lots: dict[str, dict] = {}
    for s in report.scores:
        lot = (s.parsed_rule or {}).get("lot") or "-"
        entry = lots.setdefault(lot, {"lot": lot, "score_items": 0,
                                      "total_score": 0.0,
                                      "categories": set()})
        entry["score_items"] += 1
        entry["total_score"] = round(entry["total_score"] + s.max_score, 2)
        entry["categories"].add(s.category)
    for e in lots.values():
        e["categories"] = sorted(e["categories"])
    result = {"doc_id": doc_id, "lot_count": len(lots),
              "lots": sorted(lots.values(), key=lambda x: x["lot"])}
    if len(lots) > 1:
        result["hint"] = "多分标文件：请用 get_report(doc_id, lot='分标名') 按分标查看，避免混淆"
    return json.dumps(result, ensure_ascii=False, indent=2)


_SECTIONS = ("fields", "scores", "requirements", "rejections",
             "star_clauses", "sum_checks", "evidence")


def _tool_get_report(args: dict) -> str:
    import json
    from bidmaster.orchestration.pipeline import Pipeline
    report = Pipeline(work_root=Path("work")).load_report(args["doc_id"])
    if report is None:
        raise ValueError(f"报告不存在: {args['doc_id']}")
    rd = report.model_dump(mode="json")
    section = args.get("section", "summary")
    lot = args.get("lot")
    category = args.get("category")
    page = max(int(args.get("page", 1)), 1)
    page_size = min(max(int(args.get("page_size", 50)), 1), 200)

    def _match_lot(item: dict) -> bool:
        return not lot or (item.get("parsed_rule") or {}).get("lot", "-") == lot

    def _match_cat(item: dict, key: str = "category") -> bool:
        return not category or item.get(key) == category

    out: dict = {"doc_id": rd["doc_id"], "file_name": rd["file_name"],
                 "filters": {"lot": lot, "category": category,
                             "section": section, "page": page,
                             "page_size": page_size},
                 "stats": rd.get("stats", {})}
    if section == "full":  # 兼容旧调用：全量
        return json.dumps(rd, ensure_ascii=False)
    if section in ("summary", "fields", "all"):
        out["fields"] = rd.get("fields", {})
    if section in ("summary", "scores", "all"):
        scores = [s for s in rd.get("scores", [])
                  if _match_lot(s) and _match_cat(s)]
        out["scores_total"] = len(scores)
        out["scores"] = scores[(page - 1) * page_size: page * page_size]
        if lot is None and category is None \
                and len({_lot_of(s) for s in scores}) > 1:
            out["hint"] = "检测到多个分标：建议用 list_lots 列出分标后按 lot 过滤查看"
    if section in ("summary", "requirements", "all"):
        reqs = [r for r in rd.get("requirements", []) if _match_lot(r)]
        out["requirements_total"] = len(reqs)
        out["requirements"] = reqs[(page - 1) * page_size: page * page_size]
    if section in ("rejections", "all"):
        out["rejections"] = rd.get("rejections", [])
    if section in ("sum_checks", "all"):
        out["sum_checks"] = rd.get("sum_checks", [])
    if section in ("summary", "all") and lot is None and category is None:
        out["star_clauses"] = rd.get("star_clauses", [])[:10]
        out["issues"] = rd.get("issues", [])
        out["conflicts"] = rd.get("conflicts", [])[:10]
    if section in ("evidence", "all"):
        ev = rd.get("evidence", [])
        out["evidence_total"] = len(ev)
        out["evidence"] = ev[(page - 1) * page_size: page * page_size]
    return json.dumps(out, ensure_ascii=False)


_HANDLERS = {"analyze_tender": _tool_analyze_tender, "get_report": _tool_get_report,
             "list_lots": _tool_list_lots}


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
