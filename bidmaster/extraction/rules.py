"""字段规则引擎：每个字段一组"标签正则包"，配合归一化器输出候选值（零 token）。

抽取优先级：表格标签单元格 > 锚区文本 > 全文兜底。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field as dc_field

from bidmaster.extraction.normalize import normalize_amount, normalize_datetime, normalize_duration

# 归一化器类型
NORM_TEXT = "text"
NORM_AMOUNT = "amount"      # → 元
NORM_DATETIME = "datetime"  # → ISO
NORM_DURATION = "duration"  # → 天


@dataclass
class RulePack:
    field_key: str
    patterns: list[re.Pattern] = dc_field(default_factory=list)
    normalizer: str = NORM_TEXT
    preferred_zones: list[str] = dc_field(default_factory=list)  # 锚区偏好


def _c(label: str, value_head: str = r"([^，。;；\n]{2,60}?)", value_tail: str = "") -> re.Pattern:
    """构造 `标签：值` 形态的正则。

    文本路径强制要求冒号（消灭"按招标编号顺序核定"这类裸词噪声）；
    表格 summary 行（label 与值以 \t 相邻）单独允许；值锚定到行内终止符，
    遇 地址/联系人/电话 等下一个标签时截断。
    """
    tail = value_tail or r"(?=[，。;；\n]|地址|联系人|电话|邮编|传真|邮箱|备注|$)"
    return re.compile(
        rf"(?:{label})\s*[：:]\s*{value_head}{tail}"
        rf"|(?:^|\t)(?:{label})\t\s*{value_head}{tail}",
        re.M)


# 值本身是另一个标签/占位 → 无效
_VALUE_JUNK = re.compile(r"^(?:招标编号|项目编号|标段编号|采购编号|分标编号|工程名称|分标|序号|条款|条款号)[：:]?$")


def _is_junk_value(raw: str) -> bool:
    raw = raw.strip()
    if not raw or raw.endswith(("：", ":")):
        return True
    if _VALUE_JUNK.match(raw):
        return True
    return raw in _INVALID_VALUES


FIELD_RULES: dict[str, RulePack] = {
    "project_name": RulePack(
        field_key="project_name",
        patterns=[_c(r"项目名称|工程名称|采购项目名称|标段名称"),
                  re.compile(r"就\s*(?:以下)?\s*([\u4e00-\u9fa5（）()A-Za-z0-9]+?)项目\s*(?:进行|施工|采购)", re.M)],
        preferred_zones=["notice", "instructions_front", "instructions"],
    ),
    "tender_no": RulePack(
        field_key="tender_no",
        patterns=[_c(r"招标编号|项目编号|标段编号|采购编号|招标文件编号|招标项目编号",
                     value_head=r"([A-Za-z0-9\-—－_()（）\.、/]{4,60}?)")],
        preferred_zones=["notice", "instructions_front"],
    ),
    "tenderer": RulePack(
        field_key="tenderer",
        patterns=[_c(r"招\s*标\s*人|采购人|建设单位"),
                  _c(r"招标人")],
        preferred_zones=["notice", "instructions_front"],
    ),
    "agency": RulePack(
        field_key="agency",
        patterns=[_c(r"招标代理机构|采购代理机构|招标代理")],
        preferred_zones=["notice", "instructions_front"],
    ),
    "fund_source": RulePack(
        field_key="fund_source",
        patterns=[_c(r"资金来源|资金性质|资金落实情况")],
        preferred_zones=["notice", "instructions_front"],
    ),
    "site": RulePack(
        field_key="site",
        patterns=[_c(r"建设地点|项目地点|工程地点|实施地点|交货地点|服务地点")],
        preferred_zones=["notice", "instructions_front"],
    ),
    "price_limit": RulePack(
        field_key="price_limit",
        patterns=[
            # 标签后括号内小写金额：最高投标限价：（¥250,000,000.00）
            re.compile(r"(?:最高投标限价|最高限价|招标控制价|拦标价|最高投标报价)[^\n（）()]{0,12}[（(]\s*([^（）()]{4,50})[）)]", re.M),
            _c(r"最高投标限价|最高限价|招标控制价|拦标价|最高投标报价"),
            # 预算金额单独作为兜底
            _c(r"预算金额|采购预算"),
        ],
        normalizer=NORM_AMOUNT,
        preferred_zones=["notice", "instructions_front", "instructions"],
    ),
    "budget_amount": RulePack(
        field_key="budget_amount",
        patterns=[_c(r"预算金额|采购预算|项目预算")],
        normalizer=NORM_AMOUNT,
        preferred_zones=["notice", "instructions_front"],
    ),
    "security_deposit": RulePack(
        field_key="security_deposit",
        patterns=[
            re.compile(r"(?:投标保证金|保证金)[^\n（）()]{0,10}[（(]\s*([^（）()]{4,40})[）)]", re.M),
            _c(r"投标保证金|保证金金额|保证金"),
        ],
        normalizer=NORM_AMOUNT,
        preferred_zones=["instructions_front", "instructions", "notice"],
    ),
    "bid_deadline": RulePack(
        field_key="bid_deadline",
        # 仅认明确的投标/递交/开标前缀（裸日期兜底会误抓 DL5009.2-2013 标准编号、竣工时间等）
        patterns=[re.compile(
            r"(?:投标文件递交的?截止时间|递交投标文件截止时间|投标截止时间|开标时间)[^\d二〇]{0,6}"
            r"((?:\d{4}|[零〇一二三四五六七八九]{4})\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日[^\n，。;；]{0,12})", re.M)],
        normalizer=NORM_DATETIME,
        preferred_zones=["instructions_front", "notice", "instructions"],
    ),
    "bid_validity": RulePack(
        field_key="bid_validity",
        patterns=[re.compile(r"投标有效期[^\d]{0,15}(\d{1,3})\s*(日历天|历天|工作日|天|日)", re.M)],
        normalizer=NORM_DURATION,
        preferred_zones=["instructions_front", "instructions"],
    ),
    "duration": RulePack(
        field_key="duration",
        patterns=[_c(r"计划工期|合同工期|工期要求|总工期|服务期|工期|实施周期|交货期|供货期",
                     value_head=r"([^\n，。;；]{1,30}?)(?=。|；|，|\n|$)")],
        normalizer=NORM_DURATION,
        preferred_zones=["instructions_front", "notice", "instructions"],
    ),
    "quality_standard": RulePack(
        field_key="quality_standard",
        patterns=[_c(r"质量标准|质量要求|工程质量标准",
                     value_head=r"([^\n，。;；]{2,40}?)(?=。|；|\n|$)")],
        preferred_zones=["instructions_front", "notice"],
    ),
    "evaluation_method": RulePack(
        field_key="evaluation_method",
        patterns=[_c(r"评标办法|评审办法|评标方式",
                     value_head=r"([^\n，。;；]{2,30}?)(?=。|；|\n|$)")],
        preferred_zones=["evaluation_method", "instructions_front"],
    ),
    "technical_weight": RulePack(
        field_key="technical_weight",
        patterns=[re.compile(r"技术(?:标|部分)?[^\d%。]{0,8}(\d{1,3})\s*分", re.M)],
        normalizer=NORM_TEXT,
        preferred_zones=["evaluation_method", "scoring"],
    ),
    "commercial_weight": RulePack(
        field_key="commercial_weight",
        patterns=[re.compile(r"商务(?:标|部分)?[^\d%。]{0,8}(\d{1,3})\s*分", re.M)],
        normalizer=NORM_TEXT,
        preferred_zones=["evaluation_method", "scoring"],
    ),
    "price_weight": RulePack(
        field_key="price_weight",
        patterns=[re.compile(r"(?:价格|报价)(?:标|部分)?[^\d%。]{0,8}(\d{1,3})\s*分", re.M)],
        normalizer=NORM_TEXT,
        preferred_zones=["evaluation_method", "scoring"],
    ),
    # ---------- 二期扩展字段（对标易标 18 项 / tender-extract 40 字段） ----------
    "credit_code": RulePack(
        field_key="credit_code",
        patterns=[_c(r"统一社会信用代码",
                     value_head=r"([0-9A-HJ-NPQRTUWXY]{18})")],
        preferred_zones=["notice", "instructions_front", "qualification"],
    ),
    "legal_representative": RulePack(
        field_key="legal_representative",
        patterns=[_c(r"法定代表人|法人代表"),
                  _c(r"法\s*定\s*代\s*表\s*人")],
        preferred_zones=["notice", "instructions_front"],
    ),
    "contact_phone": RulePack(
        field_key="contact_phone",
        patterns=[_c(r"联系电话|联系方式|电话|传真",
                     value_head=r"([0-9\-—－至,，、]{7,25})")],
        preferred_zones=["notice", "instructions_front"],
    ),
    "doc_get_way": RulePack(
        field_key="doc_get_way",
        patterns=[_c(r"招标文件获取方式|获取招标文件方式|招标文件的?获取",
                     value_head=r"([^\n，。;；]{2,50}?)(?=[，。;；\n]|$)")],
        preferred_zones=["notice", "instructions_front"],
    ),
    "doc_price": RulePack(
        field_key="doc_price",
        patterns=[_c(r"招标文件售价|文件售价|工本费"),
                 re.compile(r"(?:招标文件|文件)售价[^\n（）()]{0,6}[（(]\s*([^（）()]{1,20})[）)]", re.M)],
        normalizer=NORM_AMOUNT,
        preferred_zones=["notice", "instructions_front"],
    ),
    "submission_location": RulePack(
        field_key="submission_location",
        patterns=[_c(r"投标文件递交地点|递交地点|提交地点|投标文件提交地点")],
        preferred_zones=["instructions_front", "notice", "instructions"],
    ),
    "warranty_period": RulePack(
        field_key="warranty_period",
        patterns=[_c(r"质保期|保修期|质量保证期|缺陷责任期",
                     value_head=r"([^\n，。;；]{1,20}?)(?=[，。;；\n]|$)")],
        normalizer=NORM_DURATION,
        preferred_zones=["instructions_front", "tech_requirements", "notice"],
    ),
    "acceptance_requirements": RulePack(
        field_key="acceptance_requirements",
        patterns=[_c(r"验收要求|验收标准",
                     value_head=r"([^\n，。;；]{2,40}?)(?=[，。;；\n]|$)")],
        preferred_zones=["instructions_front", "tech_requirements"],
    ),
    "performance_bond": RulePack(
        field_key="performance_bond",
        patterns=[
            re.compile(r"履约保证金[^\n（）()]{0,8}[（(]\s*([^（）()]{2,30})[）)]", re.M),
            _c(r"履约保证金"),
        ],
        normalizer=NORM_AMOUNT,
        preferred_zones=["instructions", "contract", "instructions_front"],
    ),
    "deposit_refund": RulePack(
        field_key="deposit_refund",
        patterns=[_c(r"保证金退还|退还保证金|退保",
                     value_head=r"([^\n，。;；]{2,50}?)(?=[，。;；\n]|$)")],
        preferred_zones=["instructions_front", "instructions"],
    ),
    "deposit_method": RulePack(
        field_key="deposit_method",
        patterns=[_c(r"保证金缴纳方式|缴纳方式|递交方式",
                     value_head=r"([^\n，。;；]{2,40}?)(?=[，。;；\n]|$)")],
        preferred_zones=["instructions_front", "instructions"],
    ),
    "payment_terms": RulePack(
        field_key="payment_terms",
        patterns=[_c(r"付款条件|付款方式|结算方式|支付方式",
                     value_head=r"([^\n，。;；]{2,40}?)(?=[，。;；\n]|$)")],
        preferred_zones=["instructions_front", "contract"],
    ),
    "objection_clause": RulePack(
        field_key="objection_clause",
        patterns=[_c(r"异议与投诉|异议处理|提出异议",
                     value_head=r"([^\n，。;；]{2,50}?)(?=[，。;；\n]|$)")],
        preferred_zones=["instructions", "notice"],
    ),
    "evaluation_committee": RulePack(
        field_key="evaluation_committee",
        patterns=[_c(r"评标委员会(?:的)?组成|评标委员会构成",
                     value_head=r"([^\n，。;；]{2,50}?)(?=[，。;；\n]|$)")],
        preferred_zones=["evaluation_method"],
    ),
}

# 表格标签匹配（用于"投标人须知前附表"类两列表格：左标签/右值，或上标签/下值）
_TABLE_LABELS: dict[str, re.Pattern] = {
    "project_name": re.compile(r"^(项目名称|工程名称|采购项目名称|标段名称)$"),
    "tender_no": re.compile(r"^(招标编号|项目编号|标段编号|采购编号)$"),
    "tenderer": re.compile(r"^(招\s*标\s*人|采购人|建设单位)$"),
    "agency": re.compile(r"^(招标代理机构|采购代理机构|招标代理)$"),
    "fund_source": re.compile(r"^(资金来源|资金性质)$"),
    "site": re.compile(r"^(建设地点|项目地点|工程地点|实施地点)$"),
    "price_limit": re.compile(r"^(最高投标限价|最高限价|招标控制价|拦标价|预算金额)(（大写）|（小写）)?"),
    "budget_amount": re.compile(r"^(预算金额|采购预算)(（大写）|（小写）)?"),
    "security_deposit": re.compile(r"^(?:应?提交)?投标保证金(（?万元）?|金额)?$"),
    "deposit_method": re.compile(r"^(保证金缴纳方式|缴纳方式)$"),
    "bid_deadline": re.compile(r"^(投标截止时间|投标文件递交截止时间|递交投标文件截止时间|开标时间)$"),
    "bid_validity": re.compile(r"^投标有效期$"),
    "duration": re.compile(r"^(计划工期|合同工期|工期|服务期|交货期|供货期)$"),
    "quality_standard": re.compile(r"^(质量标准|质量要求)$"),
    "evaluation_method": re.compile(r"^(评标办法|评审办法)$"),
    "credit_code": re.compile(r"^(统一社会信用代码)$"),
    "legal_representative": re.compile(r"^(法定代表人|法人代表)$"),
    "contact_phone": re.compile(r"^(联系电话|联系方式|电话|传真)$"),
    "warranty_period": re.compile(r"^(质保期|保修期|质量保证期|缺陷责任期)$"),
    "performance_bond": re.compile(r"^(履约保证金)(（?万元）?)?$"),
    "doc_price": re.compile(r"^(招标文件售价|文件售价|工本费)$"),
    "submission_location": re.compile(r"^(投标文件递交地点|递交地点|提交地点)$"),
}

_TIME_CLEAN = re.compile(r"[（(]下同[）)]|[（(]即[^）)]*[）)]")


def _apply_normalizer(kind: str, value: str):
    if kind == NORM_AMOUNT:
        return normalize_amount(value)
    if kind == NORM_DATETIME:
        return normalize_datetime(_TIME_CLEAN.sub("", value))
    if kind == NORM_DURATION:
        return normalize_duration(value)
    return value.strip(), value.strip()


def rule_hit(pack: RulePack, text: str):
    """对一段文本执行规则包 → (raw_value, normalized, span) | None。

    normalized 恒为标量：amount→float(元)、datetime→ISO str、duration→'540日历天'、text→str。
    """
    for pat in pack.patterns:
        m = pat.search(text)
        if not m or m.group(1) is None:  # 无捕获组/未命中的模式跳过
            continue
        raw = m.group(1).strip()
        # 无效值/占位词/裸标签跳过（"评标办法前附表"的"前附表"、"1%"等）
        if _is_junk_value(raw):
            continue
        if pack.normalizer == NORM_AMOUNT and "%" in raw:
            continue  # "保证金为合同价的1%"不是金额
        norm = _apply_normalizer(pack.normalizer, raw)
        if norm is None:
            continue
        if pack.normalizer == NORM_DURATION and isinstance(norm, tuple):
            norm_value = f"{norm[0]}{norm[1]}"
        elif isinstance(norm, tuple):
            norm_value = norm[0]
        else:
            norm_value = norm
        if norm_value in (None, "", 0):
            continue
        if isinstance(norm_value, float) and norm_value.is_integer():
            norm_value = int(norm_value)  # 250000000.0 → 250000000，消除跨来源冲突噪声
        return raw, norm_value, m.span(1)
    return None


_INVALID_VALUES = {"无", "见", "详见", "/", "前附表", "详见第五章", "详见下表", "详见附件"}
