"""结构感知切分：按章节树切父块，父块内按 token 预算切子块（small-to-big）。

每块携带 section_path / page_nos / char 区间元数据，供检索与前端高亮。
一期主要供语义检索索引（pgvector），不阻塞主流程。
"""
from __future__ import annotations

from dataclasses import dataclass, field as dc_field

from bidmaster.schemas.document import ParsedDocument
from bidmaster.schemas.report import SectionNode

CHILD_TOKEN_BUDGET = 480   # ~350 汉字
PARENT_TOKEN_BUDGET = 2200


@dataclass
class Chunk:
    chunk_id: str
    parent_id: str | None
    section_path: str
    page_nos: list[int] = dc_field(default_factory=list)
    char_start: int = 0
    char_end: int = 0
    text: str = ""
    is_parent: bool = False


def _path_of(node: SectionNode, ancestors: list[SectionNode]) -> str:
    names = [a.title or a.no for a in ancestors] + [node.title or node.no]
    return " > ".join(t for t in names if t)


def split_document(parsed: ParsedDocument, tops: list[SectionNode]) -> list[Chunk]:
    chunks: list[Chunk] = []
    seq = 0

    def walk(nodes: list[SectionNode], ancestors: list[SectionNode]) -> None:
        nonlocal seq
        for node in nodes:
            path = _path_of(node, ancestors)
            blocks = parsed.blocks[node.block_start:node.block_end]
            texts = [b.text for b in blocks if b.text]
            if not texts:
                walk(node.children, ancestors + [node])
                continue
            parent_text = "\n".join(texts)
            seq += 1
            parent = Chunk(
                chunk_id=f"c-{seq:05d}", parent_id=None, section_path=path,
                page_nos=sorted({b.page_no for b in blocks if b.page_no}),
                char_start=blocks[0].char_start if blocks else 0,
                char_end=blocks[-1].char_end if blocks else 0,
                text=parent_text[:PARENT_TOKEN_BUDGET * 2], is_parent=True,
            )
            chunks.append(parent)
            # 子块：按段落累积到预算
            buf: list[str] = []
            size = 0
            for t in texts:
                t_size = len(t)
                if size + t_size > CHILD_TOKEN_BUDGET and buf:
                    seq += 1
                    chunks.append(Chunk(
                        chunk_id=f"c-{seq:05d}", parent_id=parent.chunk_id,
                        section_path=path, page_nos=parent.page_nos,
                        text="\n".join(buf)))
                    buf, size = [], 0
                buf.append(t)
                size += t_size
            if buf:
                seq += 1
                chunks.append(Chunk(
                    chunk_id=f"c-{seq:05d}", parent_id=parent.chunk_id,
                    section_path=path, page_nos=parent.page_nos,
                    text="\n".join(buf)))
            walk(node.children, ancestors + [node])

    # 无章节树的兜底：按固定窗口切
    if not tops:
        step = CHILD_TOKEN_BUDGET * 2
        for i in range(0, len(parsed.blocks), step):
            blocks = parsed.blocks[i:i + step]
            seq += 1
            chunks.append(Chunk(
                chunk_id=f"c-{seq:05d}", parent_id=None, section_path="（无章节结构）",
                page_nos=sorted({b.page_no for b in blocks if b.page_no}),
                text="\n".join(b.text for b in blocks)))
        return chunks

    walk(tops, [])
    return chunks
