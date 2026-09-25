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
    """构造 `标签：值` 形态的正则。默认值锚定到行内终止符，保证非贪婪捕获完整值。"""
    tail = value_tail or r"(?=[，。;；\n]|$)"
    return re.compile(rf"(?:{label})\s*[：:为是]?\s*{value_head}{tail}", re.M)


FIELD_RULES: dict[str, RulePack] = {
    "project_name": RulePack(
        field_key="project_name",
        patterns=[_c(r"项目名称|工程名称|采购项目名称|标段名称"),
                  re.compile(r"就\s*(?:以下)?\s*(?:[\u4e00-\u9fa5（）()]+?)项目", re.M)],
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
        # 直接抓"日期时间"串，避免标签后长描述干扰
        patterns=[re.compile(
            r"(?:投标文件递交的?截止时间|递交投标文件截止时间|投标截止时间|开标时间|截止时间[^。\n]{0,20}?)[^\d二〇]{0,6}"
            r"((?:\d{4}|[零〇一二三四五六七八九]{4})\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日[^\n，。;；]{0,12})", re.M),
            re.compile(
            r"((?:\d{4})[-/.]\d{1,2}[-/.]\d{1,2}[^\n，。;；]{0,10})", re.M)],
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
    "security_deposit": re.compile(r"^投标保证金(金额)?$"),
    "bid_deadline": re.compile(r"^(投标截止时间|投标文件递交截止时间|递交投标文件截止时间|开标时间)$"),
    "bid_validity": re.compile(r"^投标有效期$"),
    "duration": re.compile(r"^(计划工期|合同工期|工期|服务期|交货期|供货期)$"),
    "quality_standard": re.compile(r"^(质量标准|质量要求)$"),
    "evaluation_method": re.compile(r"^(评标办法|评审办法)$"),
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
        if not m:
            continue
        raw = m.group(1).strip()
        # 无效值/占位词跳过（"评标办法前附表"的"前附表"等）
        if not raw or raw in _INVALID_VALUES:
            continue
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
