"""要求条目建模器：把评分项/资格条款进一步拆成可核验、可匹配的 RequirementItem。

四类技术要求建模器：业绩 / 项目负责人 / 人员配备 / 工作方案（含暗标）
商务六类：资质认证 / 财务 / 信用 / 荣誉 / 承诺 / 价格公式
另有 ★号实质性条款抽取。
"""
from __future__ import annotations

import re

from bidmaster.extraction.normalize import normalize_amount
from bidmaster.schemas.document import ParsedDocument
from bidmaster.schemas.evidence import EvidenceStore, make_evidence
from bidmaster.schemas.report import AnchorZone
from bidmaster.schemas.scoring import (CAT_PRICE, REQ_COMMITMENT, REQ_CREDIT,
                                       REQ_FINANCE, REQ_HONOR, REQ_LEADER,
                                       REQ_PERFORMANCE, REQ_PLAN, REQ_PRICE,
                                       REQ_QUALIFICATION, REQ_TEAM,
                                       RequirementItem, ScoreItem, StarClause)

# ---------- 关键词注册 ----------
_PROOF_KW = ["中标通知书", "合同协议书", "合同", "竣工验收证明", "验收证明", "验收报告",
             "获奖证书", "社保证明", "社保", "职称证", "审计报告", "承诺函", "证书"]
_POS_CERTS = {
    "技术负责人": ["高级工程师", "中级工程师", "职称"],
    "施工员": ["施工员证", "岗位证书", "培训合格"],
    "安全员": ["安全生产考核合格", "安全员C证", "C证", "B证", "安全员证"],
    "质量员": ["质量员证", "岗位证书"],
    "质检员": ["质检员证", "岗位证书"],
    "材料员": ["材料员证", "岗位证书"],
    "资料员": ["资料员证", "岗位证书"],
    "造价工程师": ["注册造价工程师", "造价工程师", "一级造价"],
    "测量员": ["测量员证"],
}
_MUST_INCLUDE = ["施工组织设计", "施工方案", "总体概述", "进度计划", "质量保证",
                 "安全文明施工", "绿色施工", "应急预案", "组织机构", "资源配置",
                 "文明施工", "环境保护", "劳动力计划"]
_COMMERCIAL_CATS = [
    (REQ_QUALIFICATION, ["ISO9001", "ISO14001", "ISO45001", "质量管理体系",
                         "环境管理体系", "职业健康安全", "信用等级", "AAA"]),
    (REQ_FINANCE, ["审计报告", "财务报告", "财务状况", "资产负债", "营业收入",
                   "纳税", "税收", "银行资信"]),
    (REQ_CREDIT, ["信用中国", "失信被执行人", "重大税收违法", "严重违法失信",
                  "行贿犯罪", "失信名单"]),
    (REQ_HONOR, ["优质工程", "鲁班奖", "詹天佑", "奖项", "荣誉", "示范工程"]),
    (REQ_COMMITMENT, ["承诺函", "质保期", "保修", "服务承诺", "响应时间", "驻场"]),
]
_SCORE_KEY2REQ = [("业绩", REQ_PERFORMANCE), ("负责人", REQ_LEADER), ("项目经理", REQ_LEADER),
                  ("人员", REQ_TEAM), ("配备", REQ_TEAM), ("方案", REQ_PLAN),
                  ("组织设计", REQ_PLAN), ("资质", REQ_QUALIFICATION), ("认证", REQ_QUALIFICATION),
                  ("财务", REQ_FINANCE), ("信用", REQ_CREDIT), ("荣誉", REQ_HONOR),
                  ("奖项", REQ_HONOR), ("承诺", REQ_COMMITMENT), ("报价", REQ_PRICE),
                  ("价格", REQ_PRICE)]

_SENT_SPLIT = re.compile(r"[。；;\n]")
# 业绩时间窗：近5年 / 开标前5年内 / 投标截止日近3年 / 近3个自然年 / 前3年
_YEAR_WINDOW = re.compile(
    r"(?:近|开标\s*前|投标截止日\s*近?|截止日\s*前?|前)\s*(\d{1,3})\s*个?(?:自然年|年内|年)")
# 证书编号配对（模式分类学参考 tender-extract personnel_extractor.CERTIFICATE_PATTERNS）
_CERT_NO = re.compile(
    r"(?:建造师|工程师|安全生产考核[^，。\n]{0,8}|安全[BC]证|资格证书?|执业证|职称证)"
    r"[^，。\n]{0,24}?(?:证书)?(?:编号|号)[：:]\s*([^\s，。，；;\n]{4,30})")


def _perf_parse(sentence: str) -> dict | None:
    """业绩要求句 → 结构化约束（时间窗/金额/数量/证明材料/单项分值）。"""
    s = sentence
    if "业绩" not in s:
        return None
    if not re.search(r"完成|承担|提供|具有|获得|承接|承揽|独立", s):
        return None
    parsed: dict = {}
    m = _YEAR_WINDOW.search(s)
    if m:
        parsed["years"] = int(m.group(1))
    else:
        return None  # 无时间窗的业绩句过于宽泛，不建模
    m = re.search(r"(\d+)\s*项", s)
    if m:
        parsed["count_min"] = int(m.group(1))
    m = re.search(r"金额(?:不低于|达到|在|为)?\s*([\d一二三四五六七八九十佰仟万亿,，.]+\s*万?亿?元?)", s)
    if m:
        amt = normalize_amount(m.group(1))
        if amt:
            parsed["amount_min"] = int(amt[0]) if float(amt[0]).is_integer() else amt[0]
    proofs = [kw for kw in _PROOF_KW if kw in s]
    if proofs:
        parsed["proof_materials"] = proofs
    m = re.search(r"得\s*(\d+)\s*分", s)
    if m:
        parsed["unit_score"] = float(m.group(1))
    return parsed or None


def _team_parse(sentence: str, pos: str) -> dict | None:
    idx = sentence.find(pos)
    if idx < 0:
        return None
    window = sentence[idx: idx + 40]
    parsed: dict = {"position": pos}
    m = re.search(r"(\d+)\s*(?:名|人)", window)
    if m:
        parsed["count"] = int(m.group(1))
    hits = [c for c in _POS_CERTS.get(pos, []) if c in window]
    if hits:
        parsed["certificates"] = hits
    m = re.search(r"([一-龥]{1,4}职称|高级工程师|中级工程师|助理工程师)", window)
    if m:
        parsed["title"] = m.group(1)
    m = _CERT_NO.search(sentence)
    if m:
        parsed.setdefault("cert_numbers", []).append(m.group(1))
    return parsed if len(parsed) > 1 else None


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENT_SPLIT.split(text) if len(s.strip()) >= 4]


def _zone_blocks(parsed: ParsedDocument, zones: list[AnchorZone],
                 names: tuple[str, ...]) -> tuple[list, int, int]:
    """聚合若干锚区的块；无命中返回全文。"""
    blocks: list = []
    hit = False
    for z in zones:
        if z.zone in names:
            hit = True
            blocks.extend(parsed.blocks[z.block_start:z.block_end])
    if hit and blocks:
        return blocks, 1, 1
    return parsed.blocks, 0, 0


def _add_ev(store: EvidenceStore, b) -> str:
    ev = store.add(make_evidence(kind="block", source="rules", page_no=b.page_no,
                                 bbox=b.bbox, char_start=b.char_start,
                                 char_end=b.char_end, block_id=b.block_id,
                                 snippet=b.text[:200]))
    return ev.evidence_id


class RequirementBuilder:
    def __init__(self, parsed: ParsedDocument, zones: list[AnchorZone],
                 scores: list[ScoreItem], store: EvidenceStore):
        self.parsed = parsed
        self.zones = zones
        self.scores = scores
        self.store = store
        self.reqs: list[RequirementItem] = []
        self.stars: list[StarClause] = []

    # ---------- 主入口 ----------
    def build(self) -> tuple[list[RequirementItem], list[StarClause]]:
        self._perf()
        self._leader()
        self._team()
        self._plan()
        self._commercial()
        self._price()
        self._star()
        self._from_scores()
        self._dedupe_team()
        self._link()
        for i, r in enumerate(self.reqs, 1):
            r.req_id = f"R-{i:03d}"
        return self.reqs, self.stars

    # ---------- 从评分项细则提取（国网格式：业绩/人员要求嵌在评分表"项目内容"列） ----------
    def _from_scores(self) -> None:
        for s in self.scores:
            rule = s.rule_text or ""
            if len(rule) < 10:
                continue
            for sentence in _sentences(rule):
                # 业绩要求
                parsed = _perf_parse(sentence)
                if parsed:
                    self.reqs.append(RequirementItem(
                        req_id="", type=REQ_PERFORMANCE, subject="投标人",
                        constraint=sentence[:300], parsed=parsed,
                        scoring_refs=[s.score_id], evidence_ids=list(s.evidence_ids),
                        confidence=0.82))
                # 人员要求（窗口定位）
                for pos in _POS_CERTS:
                    parsed = _team_parse(sentence, pos)
                    if parsed:
                        self.reqs.append(RequirementItem(
                            req_id="", type=REQ_TEAM, subject=pos,
                            constraint=sentence[:300], parsed=parsed,
                            scoring_refs=[s.score_id],
                            evidence_ids=list(s.evidence_ids), confidence=0.78))
                # 项目负责人要求
                if re.search(r"项目负责人|项目经理", sentence):
                    parsed: dict = {}
                    m = re.search(r"([一-龥]{2,6}?)?(?:专业)?(?:一级|二级|壹级|贰级)?(?:注册)?建造师", sentence)
                    if m:
                        parsed["registered_builder"] = (m.group(0) or "").strip()
                    m2 = re.search(r"(高级|中级|初级)工程师", sentence)
                    if m2:
                        parsed["title"] = f"{m2.group(1)}工程师"
                    if re.search(r"无在建|在建工程|不得同时", sentence):
                        parsed["ongoing_limit"] = "无在建工程或满足文件限制"
                    if parsed:
                        self.reqs.append(RequirementItem(
                            req_id="", type=REQ_LEADER, subject="项目负责人",
                            constraint=sentence[:300], parsed=parsed,
                            scoring_refs=[s.score_id],
                            evidence_ids=list(s.evidence_ids), confidence=0.82))

    def _dedupe_team(self) -> None:
        """同一岗位多个来源时：parsed 为子集的条目并入超集条目（保留信息量大的）。"""
        by_subject: dict[str, RequirementItem] = {}
        out: list[RequirementItem] = []
        for r in self.reqs:
            if r.type != REQ_TEAM:
                out.append(r)
                continue
            keep = by_subject.get(r.subject)
            if keep is None:
                by_subject[r.subject] = r
                out.append(r)
                continue
            if set(r.parsed) - set(keep.parsed):
                # 新条目有额外字段 → 用它替换，但保留原证据
                r.evidence_ids = list(dict.fromkeys(keep.evidence_ids + r.evidence_ids))
                by_subject[r.subject] = r
                out[out.index(keep)] = r
            else:
                keep.evidence_ids = list(dict.fromkeys(keep.evidence_ids + r.evidence_ids))
        self.reqs = out

    # ---------- ① 类似工程业绩 ----------
    def _perf(self) -> None:
        blocks, _, _ = _zone_blocks(self.parsed, self.zones,
                                    ("scoring", "qualification", "evaluation_method"))
        seen: set[str] = set()
        for b in blocks:
            for s in _sentences(b.text):
                if not ("业绩" in s and ("完成" in s or "承担" in s or "提供" in s or "具有" in s)):
                    continue
                if not re.search(r"近\s*\d+\s*年|近年|类似|同类", s):
                    continue
                parsed: dict = {}
                m = re.search(r"近\s*(\d+)\s*年", s)
                if m:
                    parsed["years"] = int(m.group(1))
                m = re.search(r"(\d+)\s*项", s)
                if m:
                    parsed["count_min"] = int(m.group(1))
                m = re.search(r"金额(?:不低于|达到|在|为)?\s*([\d一二三四五六七八九十佰仟万亿,，.]+\s*万?亿?元?)", s)
                if m:
                    amt = normalize_amount(m.group(1))
                    if amt:
                        parsed["amount_min"] = amt[0]
                proofs = [kw for kw in _PROOF_KW if kw in s]
                if proofs:
                    parsed["proof_materials"] = proofs
                m = re.search(r"得\s*(\d+)\s*分", s)
                if m:
                    parsed["unit_score"] = float(m.group(1))
                # 去重：核心约束（年限/金额/数量/证明）相同即视为同一条款
                key = str((parsed.get("years"), parsed.get("amount_min"),
                           parsed.get("count_min"),
                           tuple(sorted(parsed.get("proof_materials", [])))))
                if key in seen:
                    continue
                seen.add(key)
                if isinstance(parsed.get("amount_min"), float) \
                        and parsed["amount_min"].is_integer():
                    parsed["amount_min"] = int(parsed["amount_min"])
                ev = _add_ev(self.store, b)
                self.reqs.append(RequirementItem(
                    req_id="", type=REQ_PERFORMANCE, subject="投标人", constraint=s[:300],
                    parsed=parsed, evidence_ids=[ev], confidence=0.85))

    # ---------- ② 项目负责人 ----------
    def _leader(self) -> None:
        blocks, _, _ = _zone_blocks(self.parsed, self.zones,
                                    ("scoring", "qualification", "instructions"))
        seen_spans: set[str] = set()
        for b in blocks:
            if not re.search(r"项目负责人|项目经理", b.text):
                continue
            constraint_parts: list[str] = []
            parsed: dict = {}
            for s in _sentences(b.text):
                if not re.search(r"项目负责人|项目经理", b.text + s):
                    pass
                m = re.search(r"([一-龥]{2,6}?)?(?:专业)?(?:一级|二级|壹级|贰级)?(?:注册)?建造师", s)
                if m:
                    parsed["registered_builder"] = (m.group(0) or "").strip()
                    constraint_parts.append(s)
                m2 = re.search(r"(高级|中级|初级)工程师", s)
                if m2:
                    parsed["title"] = f"{m2.group(1)}工程师"
                    constraint_parts.append(s)
                if re.search(r"无在建|在建工程|不得同时", s):
                    parsed["ongoing_limit"] = "无在建工程或满足文件限制"
                    constraint_parts.append(s)
                if re.search(r"近\s*\d+\s*年.{0,20}(完成|主持)", s):
                    m3 = re.search(r"近\s*(\d+)\s*年", s)
                    parsed["performance"] = {"years": int(m3.group(1)) if m3 else None}
                    parsed.setdefault("performance", {})["text"] = s[:120]
                    constraint_parts.append(s)
                if re.search(r"社保|养老保险", s):
                    parsed["social_security"] = True
                    constraint_parts.append(s)
            if not parsed:
                continue
            key = b.text[:50]
            if key in seen_spans:
                continue
            seen_spans.add(key)
            ev = _add_ev(self.store, b)
            self.reqs.append(RequirementItem(
                req_id="", type=REQ_LEADER, subject="项目负责人",
                constraint="；".join(dict.fromkeys(constraint_parts))[:400] or b.text[:300],
                parsed=parsed, evidence_ids=[ev], confidence=0.85))

    # ---------- ③ 工作组人员配备 ----------
    def _team(self) -> None:
        # 表格优先：含岗位名的行
        for tb in self.parsed.tables:
            header = " ".join(tb.rows[0]) if tb.rows else ""
            if not any(kw in header for kw in ("岗位", "人员", "职务", "拟投入")):
                # 兜底：表体含 ≥2 个岗位名也算人员表
                joined = " ".join(c for r in tb.rows for c in r)
                if sum(1 for p in _POS_CERTS if p in joined) < 2:
                    continue
            for ri, row in enumerate(tb.rows):
                row_text = " ".join(row)
                for pos, certs in _POS_CERTS.items():
                    if pos not in row_text:
                        continue
                    parsed: dict = {"position": pos}
                    m = re.search(r"(\d+)\s*(?:名|人)", row_text)
                    if m:
                        parsed["count"] = int(m.group(1))
                    hits = [c for c in certs if c in row_text]
                    if hits:
                        parsed["certificates"] = hits
                    m = re.search(r"([一-龥]{1,4}职称|高级工程师|中级工程师|助理工程师)", row_text)
                    if m:
                        parsed["title"] = m.group(1)
                    ev = self.store.add(make_evidence(
                        kind="table_cell", source="table",
                        page_no=(tb.page_nos[0] if tb.page_nos else 0),
                        table_id=tb.table_id, row=ri, snippet=row_text[:200]))
                    self.reqs.append(RequirementItem(
                        req_id="", type=REQ_TEAM, subject=pos,
                        constraint=row_text[:300], parsed=parsed,
                        evidence_ids=[ev.evidence_id], confidence=0.88))
        # 文本兜底（带去重与"岗位后窗口"定位，避免整行多岗位时计数错位）
        blocks, _, _ = _zone_blocks(self.parsed, self.zones, ("scoring", "tech_requirements"))
        seen_text: set[str] = set()
        for b in blocks:
            for s in _sentences(b.text):
                for pos in _POS_CERTS:
                    idx = s.find(pos)
                    if idx < 0:
                        continue
                    window = s[idx: idx + 40]  # 岗位名后 40 字符窗口内的数字/证书才归属该岗位
                    parsed = {"position": pos}
                    m = re.search(r"(\d+)\s*(?:名|人)", window)
                    if m:
                        parsed["count"] = int(m.group(1))
                    hits = [c for c in _POS_CERTS[pos] if c in window]
                    if hits:
                        parsed["certificates"] = hits
                    key = str(sorted(parsed.items(), key=lambda kv: kv[0]))
                    if key in seen_text:
                        continue
                    seen_text.add(key)
                    ev = _add_ev(self.store, b)
                    self.reqs.append(RequirementItem(
                        req_id="", type=REQ_TEAM, subject=pos, constraint=s[:300],
                        parsed=parsed, evidence_ids=[ev], confidence=0.75))

    # ---------- ④ 工作方案（含暗标格式） ----------
    def _plan(self) -> None:
        blocks, _, _ = _zone_blocks(self.parsed, self.zones, ("scoring", "format"))
        zone_text = "\n".join(b.text for b in blocks) or self.parsed.full_text
        parsed: dict = {}
        evids: list[str] = []
        # 必含章节
        found = [c for c in _MUST_INCLUDE if c in zone_text]
        if found:
            parsed["must_include"] = found
        for b in blocks:
            for s in _sentences(b.text):
                if re.search(r"暗标|不得出现.{0,40}(单位名称|姓名|标识|图章|印章)|身份标识", s):
                    parsed.setdefault("dark_bid", [])
                    if isinstance(parsed["dark_bid"], list):
                        parsed["dark_bid"].append(s[:150])
                    evids.append(_add_ev(self.store, b))
                m = re.search(r"(?:不超过|不得超过|控制在)\s*(\d{2,4})\s*(?:页|张)", s)
                if m:
                    parsed["page_limit"] = int(m.group(1))
                    evids.append(_add_ev(self.store, b))
                if re.search(r"(字体|字号).{0,30}(宋体|仿宋|黑体|楷体|小[一二三四]|\d+(\.\d+)?号)", s):
                    parsed.setdefault("format_font", [])
                    if isinstance(parsed["format_font"], list):
                        parsed["format_font"].append(s[:120])
                    evids.append(_add_ev(self.store, b))
        if parsed:
            self.reqs.append(RequirementItem(
                req_id="", type=REQ_PLAN, subject="技术标编制",
                constraint="暗标/格式/篇幅要求，详见证据", parsed=parsed,
                evidence_ids=list(dict.fromkeys(evids))[:8], confidence=0.85))

    # ---------- 商务六类 ----------
    def _commercial(self) -> None:
        blocks, _, _ = _zone_blocks(self.parsed, self.zones,
                                    ("scoring", "qualification", "evaluation_method"))
        for rtype, keywords in _COMMERCIAL_CATS:
            hits: list[str] = []
            evids: list[str] = []
            for b in blocks:
                for kw in keywords:
                    if kw in b.text:
                        for s in _sentences(b.text):
                            if kw in s and s not in hits:
                                hits.append(s)
                        if b.block_id not in {e for e in evids}:
                            evids.append(_add_ev(self.store, b))
                        break
            if hits:
                self.reqs.append(RequirementItem(
                    req_id="", type=rtype, subject=rtype,
                    constraint="；".join(hits[:5])[:400],
                    parsed={"items": hits[:10]},
                    evidence_ids=evids[:5], confidence=0.8))

    # ---------- 价格公式 ----------
    def _price(self) -> None:
        for s in self.scores:
            if s.category == CAT_PRICE and s.rule_text:
                ev = self.store.add(make_evidence(
                    kind="block", source="rules", page_no=0, snippet=s.rule_text[:200]))
                self.reqs.append(RequirementItem(
                    req_id="", type=REQ_PRICE, subject="投标报价",
                    constraint=s.rule_text[:300], parsed={"formula": s.formula or s.rule_text},
                    scoring_refs=[s.score_id], evidence_ids=[ev.evidence_id],
                    confidence=0.85))

    # ---------- ★号实质性条款 ----------
    def _star(self) -> None:
        for b in self.parsed.blocks:
            if "★" not in b.text:
                continue
            text = b.text.strip()
            if "★号条款" in text or "带★" in text:
                continue  # "带★号条款为实质性要求"这类元说明不是实质性条款本身
            ev_id = _add_ev(self.store, b)
            self.stars.append(StarClause(
                clause_id=f"STAR-{len(self.stars) + 1:02d}",
                text=text[:300], page_no=b.page_no, evidence_ids=[ev_id]))

    # ---------- 评分项 ↔ 要求条目双向关联 ----------
    def _link(self) -> None:
        for r in self.reqs:
            for keyword, rtype in _SCORE_KEY2REQ:
                if r.type != rtype:
                    continue
                for s in self.scores:
                    if keyword in s.name and r.req_id == "" and s.score_id not in r.scoring_refs:
                        r.scoring_refs.append(s.score_id)
        # req_id 此时尚未编号——用类型+subject 先关联，编号阶段在 build() 末尾补
        id_map: dict[str, str] = {}
        for i, r in enumerate(self.reqs, 1):
            id_map[f"{r.type}:{r.subject}"] = f"R-{i:03d}"
        for s in self.scores:
            for keyword, rtype in _SCORE_KEY2REQ:
                if keyword in s.name:
                    for r in self.reqs:
                        if r.type == rtype and id_map.get(f"{r.type}:{r.subject}") not in s.requirement_refs:
                            s.requirement_refs.append(id_map[f"{r.type}:{r.subject}"])
