"""编号归一化 + 章节树 + 锚区定位测试。"""
from bidmaster.schemas.document import Block, BlockStyle, ParsedDocument
from bidmaster.structure.anchors import find_anchor_zones
from bidmaster.structure.numbering import cn_ordinal_to_int, detect_numbering, strip_number
from bidmaster.structure.toc import build_section_tree, flatten


def _mk(blocks: list[tuple[str, int]]) -> ParsedDocument:
    blks = [Block(block_id=f"b-{i:03d}", type="heading" if lvl else "paragraph",
                  text=t, style=BlockStyle(heading_level=lvl))
            for i, (t, lvl) in enumerate(blocks, 1)]
    return ParsedDocument(doc_id="t", file_name="t.docx", file_type="docx", blocks=blks)


class TestNumbering:
    def test_chapter(self):
        assert detect_numbering("第三章 评标办法") == (1, "3")
        assert detect_numbering("第 3 章 评标办法") == (1, "3")

    def test_nested(self):
        assert detect_numbering("3.2 评分细则") == (3, "3.2")
        assert detect_numbering("1. 总则") == (2, "1")
        assert detect_numbering("（一）投标文件组成") == (3, "")

    def test_ordinal(self):
        assert cn_ordinal_to_int("十一") == 11
        assert cn_ordinal_to_int("二十") == 20

    def test_strip(self):
        assert strip_number("第三章 评标办法") == "评标办法"


class TestSectionTree:
    def test_tree(self):
        parsed = _mk([
            ("第一章 招标公告", 1), ("1. 招标条件", 2), ("正文...", 0),
            ("第二章 投标人须知", 1), ("2.1 投标文件的递交", 2), ("正文...", 0),
            ("第三章 评标办法", 1),
        ])
        tops = build_section_tree(parsed)
        assert len(tops) == 3
        assert tops[1].title == "投标人须知"
        assert len(tops[1].children) == 1
        flat = flatten(tops, max_level=2)
        assert len(flat) == 5


class TestAnchors:
    def test_zones(self):
        parsed = _mk([
            ("第一章 招标公告", 1), ("公告正文", 0),
            ("第二章 投标人须知", 1), ("投标人须知前附表", 2), ("表格略", 0),
            ("第三章 评标办法", 1), ("评分细则表", 2), ("细则正文", 0),
            ("第四章 合同条款", 1),
        ])
        zones = find_anchor_zones(parsed)
        names = {z.zone for z in zones}
        assert "notice" in names
        assert "instructions_front" in names
        assert "scoring" in names
        scoring = next(z for z in zones if z.zone == "scoring")
        assert scoring.block_start == 6
        assert scoring.block_end == 8  # 到下一章标题为止
