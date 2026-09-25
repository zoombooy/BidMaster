"""锚区定位：招标文件高度模板化，用关键词词典直接命中高价值区域（L3 信号）。

区域边界：从命中标题块起，到下一个级别 <= 起始标题级别的标题块为止；
无标题信号时退化为"命中块起 N 个块"的窗口。
"""
from __future__ import annotations

from bidmaster.schemas.document import ParsedDocument
from bidmaster.schemas.report import AnchorZone
from bidmaster.structure.toc import build_section_tree

# 区域注册表：区域名 → 命中关键词（标题优先，含同义表达）
ZONE_KEYWORDS: dict[str, list[str]] = {
    "notice":             ["招标公告", "投标邀请", "招标邀请书"],
    "instructions":       ["投标人须知前附表", "投标人须知"],
    "instructions_front": ["投标人须知前附表", "前附表"],
    "evaluation_method":  ["评标办法前附表", "评标办法", "评审办法", "评标程序"],
    "scoring":            ["评分细则", "评分标准", "评分表", "评分办法", "技术评分", "商务评分", "分值表"],
    "qualification":      ["资格审查", "资格要求", "投标人资格", "资格条件"],
    "tech_requirements":  ["技术要求", "技术标准", "技术规范"],
    "contract":           ["合同条款", "合同主要条款"],
    "format":             ["投标文件格式", "格式要求"],
}

_ZONE_WINDOW = 150  # 无标题边界时的兜底窗口（块数）


def _find_zone_end(blocks, start_idx: int, start_level: int) -> int:
    for j in range(start_idx + 1, len(blocks)):
        b = blocks[j]
        lvl = b.style.heading_level
        if lvl and lvl <= start_level:
            return j
    return min(start_idx + _ZONE_WINDOW, len(blocks))


def find_anchor_zones(parsed: ParsedDocument) -> list[AnchorZone]:
    sections = build_section_tree(parsed)
    # 平铺全部章节（含深层），按块区间匹配
    flat: list[tuple[int, int, str, int, str]] = []  # (block_start, block_end, section_id, level, title)

    def walk(nodes):
        for n in nodes:
            flat.append((n.block_start, n.block_end, n.section_id, n.level, n.title))
            walk(n.children)

    walk(sections)

    zones: list[AnchorZone] = []
    for zone, keywords in ZONE_KEYWORDS.items():
        for kw in keywords:
            hit = None
            # 1) 优先命中章节标题（不同语义区域允许命中同一章节，如 instructions 与 instructions_front）
            for bs, be, sid, lvl, title in flat:
                if kw in title:
                    hit = (bs, be, sid, lvl, title)
                    break
            # 2) 兜底命中块文本（如"投标人须知前附表"是表格前的段落）
            if hit is None:
                for i, b in enumerate(parsed.blocks):
                    if kw not in b.text:
                        continue
                    lvl = b.style.heading_level or 2
                    end = _find_zone_end(parsed.blocks, i, lvl)
                    hit = (i, end, "", lvl, b.text[:40])
                    break
            if hit is None:
                continue
            bs, be, sid, lvl, title = hit
            pages = [b.page_no for b in parsed.blocks[bs:be] if b.page_no]
            zones.append(AnchorZone(
                zone=zone, section_id=sid, title=title,
                block_start=bs, block_end=max(be, bs + 1),
                page_start=min(pages) if pages else 0,
                page_end=max(pages) if pages else 0,
            ))
            break  # 该区域取第一个命中关键词即可
    return zones


def zone_of(zones: list[AnchorZone], name: str) -> AnchorZone | None:
    for z in zones:
        if z.zone == name:
            return z
    return None
