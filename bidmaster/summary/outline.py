"""投标文件格式目录提取（商务/技术/价格三类目录树）。

业务背景：写标书前先整理"投标要点汇总表"，其中"投标文件构成"目录来自
招标文件末尾的"第X章 投标文件格式"章节（客户确认所有招标文件都有该章）。
本章的标题树即投标文件的组成目录；评分标准若未在该目录中体现，
按评分项名称补建目录项（generated=True 标注机器补充）。

策略（结构探测而非文档分类）：
1. 在全文标题树中找"投标文件格式"章节（任意章序号均可）；
2. 包装层（如"投标文件格式目录"）透明下钻；
3. 子树标题按关键字归入 价格/商务/技术 三个桶，其余进"其他"；
4. 与评分项对照，缺失的评分项按名称补进对应桶。
"""
from __future__ import annotations

import re

from bidmaster.schemas.document import ParsedDocument
from bidmaster.structure.toc import build_section_tree

BUCKETS = ("price", "commercial", "technical")
BUCKET_LABELS = {"price": "价格文件", "commercial": "商务文件", "technical": "技术文件"}

_BUCKET_HINTS = [
    ("price", re.compile(r"(价格|报价|投标函|开标|唱标)")),
    ("commercial", re.compile(r"(商务|资格|资质|业绩|财务|信用|证书|承诺|偏差表|保证金|保险)")),
    ("technical", re.compile(r"(技术|方案|施工组织|人员配置|服务方案)")),
]

# 标题噪音：目录里不带信息量的行（"不适用。"、纯标点等）
_JUNK_TITLE = re.compile(r"^(不适用|无|/|—|-|＿|_)?[。．.，,：:/\s]*$")

_NUM_TOP = re.compile(r"^([一二三四五六七八九十]+)[、.．]")
_NUM_SUB = re.compile(r"^（([一二三四五六七八九十]+)）")
_NUM_ARA = re.compile(r"^(\d+(?:\.\d+)*)[、.．]?\s*")

FORMAT_TITLE = re.compile(r"投标文件(格式|组成|构成|编制格式)")


def _clean(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def _split_no(title: str) -> tuple[str, str]:
    """拆出编号前缀与标题主体，如 "（一）商务偏差表" → ("（一）", "商务偏差表")。"""
    for pat in (_NUM_SUB, _NUM_TOP, _NUM_ARA):
        m = pat.match(title)
        if m:
            return m.group(0).rstrip("、.． "), title[m.end():].strip()
    return "", title


def _find_format_section(tops: list) -> tuple[object, list]:  # (SectionNode, ancestors)
    """深度优先找"投标文件格式"章节，返回 (节点, 祖先链)。"""
    stack = [(n, []) for n in reversed(tops)]
    while stack:
        node, anc = stack.pop()
        if FORMAT_TITLE.search(_clean(node.title)):
            return node, anc
        stack.extend((c, anc + [node]) for c in reversed(node.children))
    return None, []


def _walk(node) -> list[dict]:
    """递归展开子树为扁平条目（跳过包装层与噪音行），保留层级。"""
    kids = list(node.children)
    # 包装层：唯一子节点且名字就是"…目录"，直接下钻
    while len(kids) == 1 and _clean(kids[0].title).endswith("目录"):
        kids = list(kids[0].children)
    out: list[dict] = []

    def _collect(n, depth: int) -> None:
        for c in n.children:
            title = _clean(c.title)
            if not title or _JUNK_TITLE.match(title):
                continue
            no, name = _split_no(title)
            out.append({"no": no, "name": name or title,
                        "level": depth, "title": title})
            _collect(c, depth + 1)

    for k in kids:
        title = _clean(k.title)
        if not title or _JUNK_TITLE.match(title):
            continue
        no, name = _split_no(title)
        out.append({"no": no, "name": name or title,
                    "level": kids[0].level or 2, "title": title})
        _collect(k, (k.level or 2) + 1)
    return out


def extract_bid_format_outline(parsed: ParsedDocument,
                               scores: list | None = None,
                               pending_ids: set | None = None) -> dict:
    """从解析文档提取投标文件格式目录；找不到章节时按评分项兜底生成。"""
    tops = build_section_tree(parsed)
    node, _anc = _find_format_section(tops)

    buckets: dict[str, list] = {b: [] for b in BUCKETS}
    others: list[dict] = []
    source_title = ""
    found = node is not None

    if found:
        source_title = _clean(node.title)
        current = None
        root_no = re.compile(r"^[一二三四五六七八九十]+、")
        for it in _walk(node):
            title = it.pop("title", "") or it["name"]
            # 桶切换只在两处发生：编号根（"一、价格文件"）或尚未归属任何桶——
            # 桶内条目（如商务文件下的"报价偏差说明"）跟随所属桶，防误切
            m = None
            for bucket, pat in _BUCKET_HINTS:
                if pat.search(it["name"]):
                    m = bucket
                    break
            if m and (root_no.match(title) or current is None):
                current = m
            (buckets[current] if current else others).append(it)
    else:
        source_title = "未找到'投标文件格式'章节"

    # 评分项补目录：该桶已有条目且评分项名已覆盖 → 不补；否则按名称生成
    supplemented = _supplement_from_scores(buckets, scores or [],
                                           pending_ids or set())

    result = {
        "found": found,
        "source_section": source_title,
        "buckets": [{"key": b, "label": BUCKET_LABELS[b],
                     "items": buckets[b]} for b in BUCKETS],
        "others": others,
        "supplemented": supplemented,
    }
    if not found:
        result["note"] = "未在标题树中找到'投标文件格式'章节；目录由评分项与资格要求生成"
    return result


def pick_target_lot(lot_info: list[dict]) -> str:
    """单包聚焦：多包项目客户只编一个包的标书，默认取包号最小的包。
    复合键（分标::包号）适配：先按分标名排序，再比包号数字——
    不再拿分标名里恰好出现的数字（如电压等级 500）当包号。"""
    if not lot_info:
        return ""
    from bidmaster.summary.scope import identity_number, split_fenbiao

    def key(li: dict):
        name = str(li.get("lot", ""))
        fb, bare = split_fenbiao(name)
        num = identity_number(bare)   # 单包身份解析（名称内嵌包号）
        if fb:
            return (1, fb, num if num is not None else 10 ** 9, name)
        return (0, num if num is not None else 10 ** 9, name)

    return sorted(lot_info, key=key)[0].get("lot", "")


def _score_tokens(name: str) -> str:
    return re.sub(r"[（(].*?[)）]|[0-9０-９分值\.．\s]", "", name or "")


def _covered(item_names: str, score_name: str) -> bool:
    tokens = _score_tokens(score_name)
    return bool(tokens) and tokens in item_names


def _attr(s, key: str, default: str = "") -> str:
    """评分项可能是 dict（管线阶段产物）或模型对象，统一取值。"""
    if isinstance(s, dict):
        return s.get(key) or default
    return getattr(s, key, default) or default


def _supplement_from_scores(buckets: dict, scores: list,
                            pending_ids: set) -> int:
    added = 0
    cat_bucket = {"technical": "technical", "commercial": "commercial"}
    for cat, bucket in cat_bucket.items():
        items = buckets[bucket]
        joined = "".join(i["name"] for i in items)
        for s in scores:
            if _attr(s, "category") != cat or not _attr(s, "name"):
                continue
            name = _attr(s, "name")
            if _covered(joined, name):
                continue
            buckets[bucket].append({
                "no": "", "name": name, "level": 2,
                "title": name, "generated": True,
                "pending": _attr(s, "score_id") in pending_ids})
            joined += name
            added += 1
    return added
