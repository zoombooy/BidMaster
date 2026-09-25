"""表格专项处理：跨页续表合并。

判定规则（启发式，保守合并）：
  相邻两个表格满足——
  1) 前表末页 == 后表首页 - 1（页码连续）
  2) 列数一致
  3) 前表最后一行"看起来被截断"（最后一行存在空单元格）或后表首行无表头特征（首行存在空格）
  → 合并 rows 并保留 page_nos 列表。
误合并代价低于漏合并（分值合计校验会兜底），但仍保持保守阈值。
"""
from __future__ import annotations

from bidmaster.schemas.document import TableModel


def _looks_truncated_bottom(rows: list[list[str]]) -> bool:
    """前表末尾行存在空单元格 → 疑似被页底截断。"""
    if not rows:
        return False
    last = rows[-1]
    return any(c == "" for c in last)


def _first_row_incomplete(rows: list[list[str]]) -> bool:
    """后表首行存在空单元格且不像表头 → 疑似上一页续表。"""
    if not rows:
        return False
    first = rows[0]
    return any(c == "" for c in first) and not all(c for c in first)


def merge_cross_page_tables(tables: list[TableModel]) -> list[TableModel]:
    if len(tables) < 2:
        return tables
    merged: list[TableModel] = []
    for tb in tables:
        prev = merged[-1] if merged else None
        if (
            prev is not None
            and prev.page_nos and tb.page_nos
            and prev.page_nos[-1] == tb.page_nos[0] - 1
            and _ncols(prev) == _ncols(tb)
            and (_looks_truncated_bottom(prev.rows) or _first_row_incomplete(tb.rows))
        ):
            prev.rows.extend(tb.rows)
            prev.page_nos.extend(tb.page_nos)
            if tb.cell_bboxes:
                prev.cell_bboxes.extend(tb.cell_bboxes)
        else:
            merged.append(tb)
    return merged


def _ncols(tb: TableModel) -> int:
    return max((len(r) for r in tb.rows), default=0)
