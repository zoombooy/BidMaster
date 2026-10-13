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


def _fv(fields: dict, key: str) -> str:
    f = fields.get(key)
    if not f or f.get("status") != "found":
        return _MISSING
    return f.get("value_normalized") or f.get("value_raw") or ""


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
        if f.field_key in skip or f.group == "评标总纲":
            continue
        conf = f"{round(f.confidence * 100)}%" if f.status == "found" else ""
        _row(ws, r, [f.field_label,
                     f.value_normalized if f.status == "found" else _MISSING,
                     conf, f.status])
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
        price = ""
        pf = fields.get("price_limit")
        if pf and pf.get("status") == "found":
            price = pf.get("value_normalized") or pf.get("value_raw")
        dur = ""
        df = fields.get("duration")
        if df and df.get("status") == "found":
            dur = df.get("value_normalized") or df.get("value_raw")
        site = ""
        sf = fields.get("site")
        if sf and sf.get("status") == "found":
            site = sf.get("value_normalized") or sf.get("value_raw")
        _row(ws, r, [_fv(report.fields, "project_name"), lot, lot,
                     li.get("project", ""), "见招标公告", "见技术规范书",
                     site or _MISSING, dur or _MISSING,
                     price or _MISSING, price or _MISSING],
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


def _req_sheet(wb, report, target_lot: str) -> None:
    ws = _sheet(wb, "专用资格要求", [20, 14, 40, 52, 40])
    ws.cell(row=1, column=1, value="专用资格要求（按类型归并，原文见证据）").font = _TITLE_FONT
    _head(ws, 2, ["分标名称", "包号", "资质要求", "业绩要求", "人员要求"])
    grouped: dict[str, list[str]] = {}
    for req in report.requirements or []:
        t = (req.type or "").replace("REQ_", "").lower()
        text = req.constraint or ""
        if text:
            grouped.setdefault(t, []).append(text)
    col_map = {"qualification": 3, "performance": 4, "leader": 5, "team": 5}
    lots = report.lot_info or []
    lot_name = lots[0].get("lot", "") if lots else ""
    r = 3
    _row(ws, r, [
        _fv(report.fields, "project_name"),
        target_lot or lot_name or "全部包",
        "\n".join(grouped.get("qualification", [])) or _MISSING,
        "\n".join(grouped.get("performance", [])) or _MISSING,
        "\n".join(grouped.get("leader", []) + grouped.get("team", [])) or _MISSING,
    ], target=True)


# ------------------------------------------------------ 投标文件构成
def _outline_sheet(wb, report) -> None:
    ws = _sheet(wb, "投标文件构成", [10, 8, 74, 12])
    outline = getattr(report, "bid_outline", None) or {}
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
            note = "评分项补充" if it.get("generated") else ""
            _row(ws, r, [seq, it.get("level", ""), it.get("name", ""), note])
            if it.get("generated"):
                ws.cell(row=r, column=4).font = Font(color="C27803")
            r += 1
    for it in outline.get("others", []):
        seq += 1
        _row(ws, r, [seq, it.get("level", ""), it.get("name", ""), "其他"])
        r += 1


# ------------------------------------------------------ 评分/否决 sheets
def _rej_sheet(wb, report) -> None:
    ws = _sheet(wb, "初步评审标准", [8, 14, 90])
    _head(ws, 1, ["序号", "情形分类", "否决/无效条款原文"])
    for i, rj in enumerate(report.rejections or [], 1):
        _row(ws, i + 1, [i, rj.category or "", rj.text or ""])
    if not (report.rejections or []):
        _row(ws, 2, ["—", "—", "未识别到否决条款（该文件可能无否决情形表）"])


def _score_sheet(wb, report, category: str, sheet: str) -> None:
    ws = _sheet(wb, sheet, [8, 26, 10, 80, 12])
    _head(ws, 1, ["序号", "评分项", "分值", "评分标准", "状态"])
    items = [s for s in (report.scores or []) if s.category == category]
    for i, s in enumerate(items, 1):
        _row(ws, i + 1, [i, s.name, s.max_score or "",
                         s.rule_text or "", s.status])
    if not items:
        _row(ws, 2, ["—", "—", "—", "未识别到该类评分项", ""])


def build_summary_xlsx(report, target_lot: str = "") -> bytes:
    """生成投标要点汇总表；target_lot 为空时自动取报告中的目标包。"""
    target_lot = target_lot or getattr(report, "target_lot", "") or ""
    wb = openpyxl.Workbook()
    _cover(wb, report, target_lot)
    _basic_sheet(wb, report, target_lot)
    _lot_sheet(wb, report, target_lot)
    _req_sheet(wb, report, target_lot)
    _outline_sheet(wb, report)
    _rej_sheet(wb, report)
    _score_sheet(wb, report, "commercial", "商务评分标准")
    _score_sheet(wb, report, "technical", "技术评分标准")
    _score_sheet(wb, report, "price", "报价评分标准")
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
