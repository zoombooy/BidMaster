"""章节树构建：样式/字号标题 + 编号正则双信号融合，生成 SectionNode 树。"""
from __future__ import annotations

from bidmaster.schemas.document import ParsedDocument
from bidmaster.schemas.report import SectionNode
from bidmaster.structure.numbering import detect_numbering, strip_number


def _resolve_level(text: str, style_level: int) -> tuple[int, str]:
    """返回 (level, no)。样式级别优先，编号正则可精修层级并归一化编号。"""
    det = detect_numbering(text)
    if style_level:
        return (style_level, det[1] if det and det[1] else "")
    if det:
        return det
    return 0, ""


def build_section_tree(parsed: ParsedDocument) -> list[SectionNode]:
    """线性扫描标题块 → 维护栈构建树。返回顶层列表（每个顶层节点带 children）。"""
    tops: list[SectionNode] = []
    stack: list[SectionNode] = []  # 当前各级最新节点
    seq = 0

    for idx, block in enumerate(parsed.blocks):
        style_level = block.style.heading_level
        if block.type != "heading" and not style_level:
            continue
        level, no = _resolve_level(block.text, style_level)
        if level == 0:
            continue
        seq += 1
        node = SectionNode(
            section_id=f"sec-{seq:03d}", no=no,
            title=strip_number(block.text)[:80], level=level,
            page_start=block.page_no, page_end=block.page_no,
            block_start=idx, block_end=len(parsed.blocks),
        )
        # 弹出栈中 >= 当前级别的节点，并闭合其区间
        while stack and stack[-1].level >= level:
            done = stack.pop()
            done.block_end = idx
            done.page_end = max(
                (parsed.blocks[j].page_no for j in range(done.block_start, idx)),
                default=done.page_start)
            if done.level == 1 and done not in tops:
                pass  # tops 在压栈时统一登记
        if level == 1 or not stack:
            tops.append(node)
        else:
            stack[-1].children.append(node)
        stack.append(node)

    # 闭合剩余栈
    while stack:
        done = stack.pop()
        done.block_end = len(parsed.blocks)
        done.page_end = max(
            (parsed.blocks[j].page_no for j in range(done.block_start, len(parsed.blocks))),
            default=done.page_start)
    return tops


def flatten(tops: list[SectionNode], max_level: int = 2) -> list[SectionNode]:
    """展平到 max_level 层，供报告输出。"""
    out: list[SectionNode] = []

    def walk(nodes: list[SectionNode], depth: int) -> None:
        for n in nodes:
            shallow = n.model_copy(deep=True)
            shallow.children = []
            out.append(shallow)
            if depth + 1 < max_level and n.children:
                walk(n.children, depth + 1)

    walk(tops, 0)
    return out
