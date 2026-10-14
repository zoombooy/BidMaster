"""投标要点汇总表 xlsx 生成（openpyxl，零新增依赖）。

交付物对齐客户模板（投标要点汇总表 8 个 sheet）：
签署页 / 基本信息一览表 / 招标需求一览表 / 投标文件构成 /
初步评审标准 / 商务评分标准 / 技术评分标准 / 报价评分标准。

数据全部来自管线报告（fields / lot_fields / lot_info / scores /
requirements / rejections / bid_outline），未提取到的格子如实标注
"未提取到"，不编造。多包项目按 target_lot 聚焦：需求一览表目标包置顶
并高亮，其余包仍列出供人工参考（客户只生成一个包的标书）。
"""
from __future__ import annotations

import io
import re
from datetime import datetime

import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

_HEADER_FILL = PatternFill("solid", fgColor="DCE6F1")
_TARGET_FILL = PatternFill("solid", fgColor="FFF2CC")
_TITLE_FONT = Font(bold=True, size=14)
_HEADER_FONT = Font(bold=True)
_THIN = Side(style="thin", color="B8C4D8")
_BORDER = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)
_WRAP = Alignment(vertical="top", wrap_text=True)
_MISSING = "未提取到，请人工补录"


def _sheet(wb, name: str, widths: list[int]) -> openpyxl.Worksheet:
    ws = wb.create_sheet(name)
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    return ws


def _head(ws, row: int, cols: list[str], fill=None) -> None:
    fill = fill or _HEADER_FILL
    for c, text in enumerate(cols, 1):
        cell = ws.cell(row=row, column=c, value=text)
        cell.font = _HEADER_FONT
        cell.fill = fill
        cell.border = _BORDER
        cell.alignment = _WRAP


def _row(ws, row: int, values: list, target: bool = False) -> None:
    for c, v in enumerate(values, 1):
        cell = ws.cell(row=row, column=c, value=v)
        cell.border = _BORDER
        cell.alignment = _WRAP
        if target:
            cell.fill = _TARGET_FILL


def _attr(obj, key: str, default=""):
    """字段/评分/要求可能是模型对象（load_report）或 dict，统一取值。"""
    if isinstance(obj, dict):
        v = obj.get(key, default)
    else:
        v = getattr(obj, key, default)
    return v if v is not None else default


def _fv(fields: dict, key: str) -> str:
    f = fields.get(key)
    if not f or _attr(f, "status") != "found":
        return _MISSING
    return _attr(f, "value_normalized") or _attr(f, "value_raw") or ""


def _cn_num(n: int) -> str:
    digits = "零一二三四五六七八九"
    if n <= 10:
        return ("十" if n == 10 else ("十" + digits[n] if n > 10 else digits[n]))
    if n < 20:
        return "十" + digits[n % 10]
    return str(n)


# ---------------------------------------------------------------- 签署页
def _cover(wb, report, target_lot: str) -> None:
    ws = wb.active
    ws.title = "签署页"
    ws.column_dimensions["A"].width = 26
    ws.column_dimensions["B"].width = 72
    ws["A1"] = "投标要点汇总表"
    ws["A1"].font = _TITLE_FONT
    rows = [
        ("项目名称", _fv(report.fields, "project_name")),
        ("招标编号", _fv(report.fields, "tender_no")),
        ("目标包（本表聚焦）", target_lot or "—（单包或未识别分标）"),
        ("生成时间", datetime.now().strftime("%Y-%m-%d %H:%M")),
        ("数据来源", f"BidMaster 解析报告 {report.doc_id}（{report.file_name}）"),
        ("使用说明", "本表由 Agent 自动识别生成，供标书编制前人工复核；"
                  "标“未提取到”的格子请对照原文补录，点击 UI 证据编号可回查原文。"),
    ]
    for r, (k, v) in enumerate(rows, start=3):
        ws.cell(row=r, column=1, value=k).font = _HEADER_FONT
        ws.cell(row=r, column=2, value=v).alignment = _WRAP


# ------------------------------------------------------ 基本信息一览表
def _basic_sheet(wb, report, target_lot: str) -> None:
    ws = _sheet(wb, "基本信息一览表", [22, 46, 14, 18])
    _head(ws, 1, ["项目", "内容", "置信度", "状态"])
    skip = {"project_name"}
    r = 2
    for f in report.fields.values():
        if _attr(f, "field_key") in skip or _attr(f, "group") == "评标总纲":
            continue
        status = _attr(f, "status")
        conf = f"{round(float(_attr(f, 'confidence') or 0) * 100)}%" if status == "found" else ""
        _row(ws, r, [_attr(f, "field_label"),
                     _attr(f, "value_normalized") if status == "found" else _MISSING,
                     conf, status])
        r += 1
    # 分标信息块（对齐客户模板第一块）
    lots = report.lot_info or []
    r += 1
    ws.cell(row=r, column=1, value="分标编号").font = _HEADER_FONT
    ws.cell(row=r, column=2, value=_fv(report.fields, "tender_no"))
    ws.cell(row=r + 1, column=1, value="分标名称").font = _HEADER_FONT
    ws.cell(row=r + 1, column=2, value=_fv(report.fields, "project_name"))
    ws.cell(row=r + 2, column=1, value="分包数量").font = _HEADER_FONT
    ws.cell(row=r + 2, column=2, value=len(lots) if lots else "—")
    ws.cell(row=r + 3, column=1, value="是否允许分包").font = _HEADER_FONT
    ws.cell(row=r + 3, column=2, value=_MISSING)


# ------------------------------------------------------ 招标需求一览表
def _lot_sheet(wb, report, target_lot: str) -> None:
    ws = _sheet(wb, "招标需求一览表", [20, 8, 34, 34, 18, 16, 16, 16, 16, 16])
    ws.cell(row=1, column=1, value="附件2：招标项目及专用资格要求一览表").font = _TITLE_FONT
    cols = ["分标名称", "包号", "包名称", "项目名称", "国网采购申请号",
            "招标范围", "建设地点", "工期/服务期",
            "单项最高限价（万元）", "包最高限价（万元）"]
    _head(ws, 2, cols)
    lots = sorted(report.lot_info or [],
                  key=lambda x: _lot_sort_key(x.get("lot", ""), target_lot))
    lf = report.lot_fields or {}
    r = 3
    for li in lots:
        lot = li.get("lot", "")
        is_target = lot == target_lot
        fields = lf.get(lot, {})

        def _lv(key: str) -> str:
            f = fields.get(key)
            if f and _attr(f, "status") == "found":
                return _attr(f, "value_normalized") or _attr(f, "value_raw") or ""
            return ""

        def _lv_wan(key: str) -> str:
            """限价列头是（万元）：内部归一化值为元，导出用 Decimal 精确换算，
            不做两位四舍五入（忠实原始限价精度）；非纯数值原样返回。"""
            v = _lv(key)
            if not v:
                return ""
            try:
                from decimal import Decimal
                return format((Decimal(v) / Decimal(10000)).normalize(), "f")
            except Exception:  # noqa: BLE001 非数值（如"见技术规范书"）原样
                return v

        pkg_name = _lv("package_name") or lot
        pkg_no = _lv("package_no") or lot
        # 客户模板逐字段落实业务含义：未抽取到的格子如实标注；
        # 包限价缺失即标缺失（单项限价不能替代包限价）
        proj = _lv("project_name") or li.get("project", "") or _MISSING
        _row(ws, r, [_fv(report.fields, "project_name"), pkg_no, pkg_name,
                     proj,
                     _lv("apply_no") or _MISSING,
                     _lv("scope") or _MISSING,
                     _lv("site") or _MISSING,
                     _lv("duration") or _MISSING,
                     _lv_wan("unit_price_limit") or _MISSING,
                     _lv_wan("lot_price_limit") or _MISSING],
             target=is_target)
        r += 1
    if not lots:
        _row(ws, r, [_MISSING] * len(cols))


def _lot_sort_key(name: str, target: str):
    if name == target:
        return (0, name)
    m = re.search(r"\d+", name or "")
    return (1, int(m.group()) if m else 10 ** 9, name)


# ------------------------------------------------------ 专用资格要求
_REQ_TYPE_MAP = [("qualification", "资质要求"), ("performance", "业绩要求"),
                 ("leader", "人员要求"), ("team", "人员要求"),
                 ("finance", "财务要求"), ("credit", "信用要求")]


_REQ_TYPE_LABELS = {"performance": "业绩要求", "leader": "人员要求", "team": "人员要求",
                    "qualification": "资质要求", "finance": "财务能力", "credit": "信用要求",
                    "honor": "奖项荣誉", "commitment": "服务承诺", "plan": "编制要求",
                    "price": "报价要求", "star_clause": "实质性条款"}


def _usage_label(r) -> str:
    """用途×内容组合标签：评分要求-业绩要求 / 标书要求-人员简历；双标→"评分/标书-…"。"""
    usage = []
    if _attr(r, "usage_scoring"):
        usage.append("评分要求")
    if _attr(r, "usage_document"):
        usage.append("标书要求")
    if not usage:
        return ""
    content = _REQ_TYPE_LABELS.get(_attr(r, "type"), _attr(r, "type"))
    if "标书要求" in usage and _attr(r, "type") in ("leader", "team"):
        content = "人员简历"
    return "/".join(usage) + "-" + content


def _req_sheet(wb, report, target_lot: str) -> None:
    ws = _sheet(wb, "专用资格要求", [6, 18, 10, 10, 12, 78, 14])
    ws.cell(row=1, column=1, value="专用资格要求（模板三列）").font = _TITLE_FONT
    _head(ws, 2, ["", "分标名称", "包号", "资质要求", "业绩要求", "人员要求", ""])
    grouped: dict[str, list[str]] = {}
    for req in report.requirements or []:
        t = (_attr(req, "type") or "").replace("REQ_", "").lower()
        text = _attr(req, "constraint")
        if text:
            grouped.setdefault(t, []).append(text)
    lots = report.lot_info or []
    lot_name = lots[0].get("lot", "") if lots else ""
    caveat = ("要求按全部分标汇总（条目尚未按包归属区分），"
              "适用范围请人工确认" if (report.requirements or []) else "")
    if caveat:
        ws.cell(row=3, column=1, value=caveat).font = Font(color="C27803")
    _row(ws, 4, ["", _fv(report.fields, "project_name"),
                 target_lot or lot_name or "全部包",
                 "\n".join(grouped.get("qualification", [])) or _MISSING,
                 "\n".join(grouped.get("performance", [])) or _MISSING,
                 "\n".join(grouped.get("leader", []) + grouped.get("team", [])) or _MISSING,
                 ""], target=True)
    # 块2：用途×内容分类清单（评分要求=怎么拿分；标书要求=备什么材料；可双标）
    r0 = 5
    ws.cell(row=r0, column=1,
            value="要求分类清单（用途×内容；评分要求/标书要求 可双标）").font = _TITLE_FONT
    _head(ws, r0 + 1, ["序号", "用途-内容", "状态", "", "", "要求原文", "关联评分项"])
    reqs = report.requirements or []
    for i, req in enumerate(reqs, 1):
        refs = _attr(req, "scoring_refs")
        _row(ws, r0 + 1 + i, [
            i, _usage_label(req) or "—", _attr(req, "status"), "", "",
            _attr(req, "constraint"),
            "、".join(refs) if isinstance(refs, list) else ""])
    if not reqs:
        _row(ws, r0 + 2, ["—", "—", "", "", "", "未识别到要求", ""])


def _outline_for(report, target_lot: str, parsed=None) -> dict:
    """目录视图：与本次导出目标包一致。无法重算时回退报告缓存结果。"""
    outline = getattr(report, "bid_outline", None)
    if parsed is None:
        return outline or {}
    try:
        from bidmaster.summary.outline import extract_bid_format_outline
        from bidmaster.summary.scope import filter_for_target, scope_pending
        scores = report.scores or []
        if target_lot:
            keep, _dropped = filter_for_target(scores, target_lot)
            pending = {_attr(s, "score_id") for s in scope_pending(scores, target_lot)}
            return extract_bid_format_outline(parsed, keep, pending_ids=pending)
        return extract_bid_format_outline(parsed, scores)
    except Exception:  # noqa: BLE001 重算失败回退缓存
        return outline or {}


# ------------------------------------------------------ 投标文件构成
def _outline_sheet(wb, outline: dict) -> None:
    ws = _sheet(wb, "投标文件构成", [10, 8, 74, 12])
    outline = outline or {}
    ws.cell(row=1, column=1,
            value=f"目录来源：{outline.get('source_section') or '—'}"
            ).font = _TITLE_FONT
    r = 2
    _head(ws, r, ["序", "层级", "目录条目", "备注"])
    r += 1
    seq = 0
    for bucket in outline.get("buckets", []):
        if not bucket.get("items"):
            continue
        cell = ws.cell(row=r, column=1, value=bucket.get("label", ""))
        cell.font = _HEADER_FONT
        cell.fill = _HEADER_FILL
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=4)
        r += 1
        for it in bucket["items"]:
            seq += 1
            note = ("评分项补充·适用范围待确认" if it.get("pending")
                    else "评分项补充" if it.get("generated") else "")
            _row(ws, r, [seq, it.get("level", ""), it.get("name", ""), note])
            if it.get("generated"):
                ws.cell(row=r, column=4).font = Font(color="C27803")
            r += 1
    for it in outline.get("others", []):
        seq += 1
        _row(ws, r, [seq, it.get("level", ""), it.get("name", ""), "其他"])
        r += 1
    # 标书资料清单：标书构成类要求（应附材料/格式）→ 编标备料核对单
    doc_reqs = [rq for rq in (report.requirements or []) if _attr(rq, "usage_document")]
    if doc_reqs:
        r += 1
        ws.cell(row=r, column=1,
                value="标书资料清单（来自标书构成类要求，编标备料核对）").font = _TITLE_FONT
        r += 1
        _head(ws, r, ["序", "内容类别", "要求原文（材料/格式）", "备注"])
        for i, rq in enumerate(doc_reqs, 1):
            _row(ws, r + i, [i, _REQ_TYPE_LABELS.get(_attr(rq, "type"),
                _attr(rq, "type")), _attr(rq, "constraint"), "待备料"])


# ------------------------------------------------------ 评分/否决 sheets
def _rej_sheet(wb, report) -> None:
    ws = _sheet(wb, "初步评审标准", [8, 14, 90])
    _head(ws, 1, ["序号", "情形分类", "否决/无效条款原文"])
    for i, rj in enumerate(report.rejections or [], 1):
        _row(ws, i + 1, [i, _attr(rj, "category"), _attr(rj, "text")])
    if not (report.rejections or []):
        _row(ws, 2, ["—", "—", "未识别到否决条款（该文件可能无否决情形表）"])


def _score_sheet(wb, report, category: str, sheet: str,
                 target_lot: str = "") -> None:
    """单包贯穿（统一范围模型）：目标包交付物 = 项目通用 + 分标通用 + 目标包专属；
    仅双方包号明确不等的条目排除，归属不明保留并标"待确认"。"""
    from bidmaster.summary.scope import classify, filter_for_target, read_lot
    all_items = [s for s in (report.scores or []) if _attr(s, "category") == category]
    items, deferred = all_items, []
    filter_note = ""
    if target_lot and all_items:
        items, deferred = filter_for_target(all_items, target_lot)
        if deferred:
            filter_note = (f"已按目标包 {target_lot} 过滤掉 "
                           f"{len(deferred)} 条明确属于其他包的评分项")
    ws = _sheet(wb, sheet, [8, 26, 10, 80, 12, 14])
    head_row = 1
    if filter_note:
        ws.cell(row=1, column=1, value=filter_note).font = _TITLE_FONT
        head_row = 2  # 说明行与表头/数据行分开，避免覆盖
    _head(ws, head_row, ["序号", "评分项", "分值", "评分标准", "状态", "适用范围"])
    for i, s in enumerate(items, 1):
        if target_lot:
            scope, label = classify(read_lot(s), target_lot)
        else:
            scope, label = "unknown", (read_lot(s) or "待确认")
        _row(ws, head_row + i, [i, _attr(s, "name"), _attr(s, "max_score"),
                                _attr(s, "rule_text"), _attr(s, "status"),
                                label], target=(scope == "target"))
    if not items:
        _row(ws, head_row + 1, ["—", "—", "—",
                                "未识别到该类评分项" + ("（或已全部被单包过滤）"
                                if filter_note else ""), "", ""])


def build_summary_xlsx(report, target_lot: str = "", parsed=None) -> bytes:
    """生成投标要点汇总表；target_lot 为空时自动取报告中的目标包。

    parsed（02_parsed 的模型对象）可选传入：切换目标包导出时按本次目标包
    重算投标文件构成目录，避免复用报告生成时按旧目标包筛选的缓存结果。
    """
    target_lot = target_lot or getattr(report, "target_lot", "") or ""
    outline = _outline_for(report, target_lot, parsed)
    wb = openpyxl.Workbook()
    _cover(wb, report, target_lot)
    _basic_sheet(wb, report, target_lot)
    _lot_sheet(wb, report, target_lot)
    _req_sheet(wb, report, target_lot)
    _outline_sheet(wb, outline)
    _rej_sheet(wb, report)
    _score_sheet(wb, report, "commercial", "商务评分标准", target_lot)
    _score_sheet(wb, report, "technical", "技术评分标准", target_lot)
    _score_sheet(wb, report, "price", "报价评分标准", target_lot)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
