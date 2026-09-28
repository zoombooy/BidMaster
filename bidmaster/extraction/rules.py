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
class PatternDef:
    """分层模式：同一字段的多个正则按置信度分层（借鉴 tender-extract patterns.py）。

    精确层 0.95+（标注式）> 带语境标签 0.90 > 括号/符号 0.85 > 宽松 0.80 > 启发式 0.65-0.75。
    同一字段多变体覆盖不同表述，候选置信度 = 来源基准 × 模式置信度。
    """
    regex: re.Pattern
    conf: float
    desc: str = ""


def P(pattern: str, conf: float = 0.9, desc: str = "", flags: int = re.M) -> PatternDef:
    return PatternDef(re.compile(pattern, flags), conf, desc)


# 标准值尾部锚定：行内终止符 + 下一标签词
_TAIL = r"(?=[，。;；\n]|地址|联系人|电话|邮编|传真|邮箱|备注|$)"


def _std(label: str, vh: str = r"([^\n，。;；\n]{2,60}?)", conf: float = 0.9,
         desc: str = "") -> PatternDef:
    """`标签：值` 标准分层模式（冒号必需 + 终止锚定）。"""
    return P(rf"(?:{label})\s*[：:]\s*{vh}{_TAIL}", conf, desc or f"标注-{label}")


@dataclass
class RulePack:
    field_key: str
    patterns: list = dc_field(default_factory=list)  # list[PatternDef]
    normalizer: str = NORM_TEXT
    preferred_zones: list[str] = dc_field(default_factory=list)  # 锚区偏好
    ocr_space_fix: bool = False  # OCR 文本字母/数字间空格修复（匹配前对文本生效）

    def __post_init__(self):
        # 兼容：裸 re.Pattern 自动包装为 conf=0.9
        self.patterns = [p if isinstance(p, PatternDef)
                         else PatternDef(p, 0.9, "") for p in self.patterns]


def _c(label: str, value_head: str = r"([^，。;；\n]{2,60}?)", value_tail: str = "") -> re.Pattern:
    """构造 `标签：值` 形态的正则，支持三种形态：
    1) 行内 `标签：值`（冒号必需，消灭裸词噪声）
    2) 表格 summary 行 `label\\t值`
    3) 跨行 `标签\\n值`（OCR/表格解析后标签与值常被切分到相邻块）
    值锚定到行内终止符与下一标签（地址/联系人/电话等）。"""
    tail = value_tail or r"(?=[，。;；\n]|地址|联系人|电话|邮编|传真|邮箱|备注|$)"
    return re.compile(
        rf"(?:{label})\s*(?:[：:]|为|是)\s*{value_head}{tail}"
        rf"|(?:^|\t)(?:{label})\t\s*{value_head}{tail}"
        rf"|(?:^|\n)(?:{label})\s*：?\s*\n\s*{value_head}{tail}",
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
    # —— 项目信息 ——
    "project_name": RulePack(
        field_key="project_name",
        patterns=[
            _std(r"项目名称|工程名称|采购项目名称|标段名称", conf=0.95, desc="标注-项目名称"),
            P(r"^([^\n：]{6,55}?(?:建设项目|工程|采购项目))\s*(?:施工|监理|设计|勘察|服务)?(?:招标|采购)\s*$",
              0.88, "标题行-XX项目(施工)招标"),
            P(r"就\s*(?:以下)?\s*([\u4e00-\u9fa5（）A-Za-z0-9]+?)项目\s*(?:进行|施工|采购)",
              0.80, "就...项目进行"),
        ],
        preferred_zones=["notice", "instructions_front", "instructions"],
    ),
    "tender_no": RulePack(
        field_key="tender_no", ocr_space_fix=True,
        patterns=[
            _std(r"招标编号|项目编号|招标项目编号",
                 vh=r"([A-Za-z0-9][A-Za-z0-9\-_/（）()\.、]{3,40}?)", conf=0.95, desc="标注-招标编号"),
            _std(r"标段编号|采购编号|招标文件编号",
                 vh=r"([A-Za-z0-9][A-Za-z0-9\-_/（）()\.、]{3,40}?)", conf=0.90, desc="标注-标段/采购编号"),
            P(r"[（(](?:招标编号|项目编号)[：:]?\s*([A-Za-z0-9][A-Za-z0-9\-_/]{3,30})\s*[）)]",
              0.85, "括号内编号"),
        ],
        preferred_zones=["notice", "instructions_front"],
    ),
    "tenderer": RulePack(
        field_key="tenderer",
        patterns=[
            P(r"招\s*标\s*人[：:]\s*([^\n，。;；]{4,30}(?:有限公司|集团|公司|企业|局|中心|医院|大学|研究院|研究所|人民政府|委员会))",
              0.95, "标注-招标人+机构后缀"),
            _std(r"采购人", conf=0.93, desc="标注-采购人"),
            _std(r"建设单位", conf=0.90, desc="标注-建设单位"),
            _std(r"招\s*标\s*人", conf=0.85, desc="标注-招标人(无后缀锚定)"),
        ],
        preferred_zones=["notice", "instructions_front"],
    ),
    "agency": RulePack(
        field_key="agency",
        patterns=[
            P(r"招标代理机构[：:]\s*([^\n，。;；]{4,30}(?:有限公司|集团|公司))",
              0.95, "标注-代理机构+公司后缀"),
            _std(r"采购代理机构", conf=0.92, desc="标注-采购代理"),
            _std(r"招标代理", conf=0.88, desc="标注-招标代理"),
        ],
        preferred_zones=["notice", "instructions_front"],
    ),
    "legal_representative": RulePack(
        field_key="legal_representative",
        patterns=[_std(r"法定代表人|法人代表", conf=0.95, desc="标注-法定代表人")],
        preferred_zones=["notice", "instructions_front"],
    ),
    "contact_phone": RulePack(
        field_key="contact_phone",
        patterns=[
            P(r"(?:联系电话|联系方式)[：:]\s*((?:1[3-9]\d{9})|(?:0\d{2,3}-?\d{7,8}))",
              0.95, "标注-规范电话号码"),
            _std(r"联系电话|电话|传真", vh=r"([0-9\-—－至,，、]{7,25})", conf=0.85, desc="宽松-电话"),
        ],
        preferred_zones=["notice", "instructions_front"],
    ),
    "credit_code": RulePack(
        field_key="credit_code", ocr_space_fix=True,
        patterns=[_std(r"统一社会信用代码",
                       vh=r"([0-9A-HJ-NPQRTUWXY]{18})", conf=0.95, desc="标注-信用代码")],
        preferred_zones=["notice", "instructions_front", "qualification"],
    ),
    "fund_source": RulePack(
        field_key="fund_source",
        patterns=[_std(r"资金来源|资金性质", conf=0.92, desc="标注-资金来源")],
        preferred_zones=["notice", "instructions_front"],
    ),
    "site": RulePack(
        field_key="site",
        patterns=[_std(r"建设地点|项目地点|工程地点|实施地点", conf=0.92, desc="标注-建设地点")],
        preferred_zones=["notice", "instructions_front"],
    ),
    # —— 价格 ——
    "price_limit": RulePack(
        field_key="price_limit",
        patterns=[
            # 括号内小写金额（最可靠）
            P(r"(?:最高投标限价|最高限价|招标控制价|拦标价|最高投标报价)[^\n（）()]{0,12}[（(]\s*([^（）()]{4,50})[）)]",
              0.98, "括号-最高限价小写"),
            # 标注 + 大写金额
            P(r"(?:最高投标限价|最高限价|招标控制价|拦标价)[：:]\s*(?:人民币)?\s*([零〇一二两三四五六七八九壹贰叁肆伍陆柒捌玖拾佰仟万亿]+(?:元[整]?)?)",
              0.95, "标注-最高限价大写"),
            # 标注 + 数字金额
            P(r"(?:最高投标限价|最高限价|招标控制价|拦标价)[：:]\s*(?:人民币)?\s*(\d[\d,，]*(?:\.\d+)?)\s*(?:万?元)",
              0.93, "标注-最高限价数字"),
            # 标注+括号注记+符号金额：'最高投标限价（人民币）：¥86,000,000'
            P(r"(?:最高投标限价|最高限价|招标控制价|拦标价)[^\n\d]{0,20}[：:]?\s*[¥￥]?\s*(\d[\d,，]*(?:\.\d+)?)\s*(?:万?元)?",
              0.90, "标注+符号-金额"),
            # 预算金额单独兜底
            P(r"(?:预算金额|采购预算)[：:]\s*(?:人民币)?\s*([零〇一二两三四五六七八九壹贰叁肆伍陆柒捌玖拾佰仟万亿\d,，.]+(?:万|亿)?元?)",
              0.85, "标注-预算金额"),
        ],
        normalizer=NORM_AMOUNT,
        preferred_zones=["notice", "instructions_front", "instructions"],
    ),
    "budget_amount": RulePack(
        field_key="budget_amount",
        patterns=[
            P(r"(?:预算金额|采购预算|项目预算)[^\n（）()]{0,8}[（(]\s*([^（）()]{4,40})[）)]",
              0.93, "括号-预算金额"),
            _std(r"预算金额|采购预算|项目预算", conf=0.90, desc="标注-预算金额"),
        ],
        normalizer=NORM_AMOUNT,
        preferred_zones=["notice", "instructions_front"],
    ),
    "security_deposit": RulePack(
        field_key="security_deposit",
        patterns=[
            P(r"(?:投标保证金|保证金金额)[^\n（）()]{0,8}[（(]\s*([^（）()]{2,40})[）)]",
              0.95, "括号-投标保证金"),
            P(r"投标保证金[：:]\s*(?:人民币)?\s*([零〇一二两三四五六七八九壹贰叁肆伍陆柒捌玖拾佰仟万亿\d,，.]+(?:万|亿)?元?)",
              0.93, "标注-投标保证金"),
            _std(r"保证金金额", conf=0.88, desc="标注-保证金金额"),
            _std(r"保证金", conf=0.80, desc="宽松-保证金"),
        ],
        normalizer=NORM_AMOUNT,
        preferred_zones=["instructions_front", "instructions", "notice"],
    ),
    # —— 时间 ——
    "bid_deadline": RulePack(
        field_key="bid_deadline", ocr_space_fix=True,
        patterns=[
            P(r"投标文件递交的?截止时间[^。\n\d]{0,10}((?:\d{4}|[零〇一二三四五六七八九]{4})\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日[^\n，。;；]{0,12})",
              0.95, "递交截止-中文日期"),
            P(r"投标截止时间[^。\n\d]{0,10}((?:\d{4}|[零〇一二三四五六七八九]{4})\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日[^\n，。;；]{0,12})",
              0.95, "投标截止-中文日期"),
            P(r"开标时间[^。\n\d]{0,10}((?:\d{4}|[零〇一二三四五六七八九]{4})\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日[^\n，。;；]{0,12})",
              0.90, "开标时间-中文日期"),
        ],
        normalizer=NORM_DATETIME,
        preferred_zones=["instructions_front", "notice", "instructions"],
    ),
    "bid_validity": RulePack(
        field_key="bid_validity", ocr_space_fix=True,
        patterns=[P(r"投标有效期[^\d]{0,15}(\d{1,3}\s*(?:日历天|历天|工作日|天|日))",
                    0.95, "标注-投标有效期")],
        normalizer=NORM_DURATION,
        preferred_zones=["instructions_front", "instructions"],
    ),
    # —— 工期质量 ——
    "duration": RulePack(
        field_key="duration",
        patterns=[
            P(r"计划工期[：:]\s*([^\n，。;；]{2,25}?)(?=[，。;；\n]|$)", 0.95, "标注-计划工期"),
            P(r"合同工期[：:]\s*([^\n，。;；]{2,25}?)(?=[，。;；\n]|$)", 0.93, "标注-合同工期"),
            P(r"工期要求[：:]\s*([^\n，。;；]{2,25}?)(?=[，。;；\n]|$)", 0.90, "标注-工期要求"),
            _std(r"服务期|实施周期|交货期|供货期", conf=0.88, desc="标注-服务期"),
            _std(r"总?工期", conf=0.85, desc="标注-工期"),
        ],
        normalizer=NORM_DURATION,
        preferred_zones=["instructions_front", "notice", "instructions"],
    ),
    "quality_standard": RulePack(
        field_key="quality_standard",
        patterns=[
            _std(r"质量标准|工程质量标准|质量要求",
                 vh=r"([^\n，。;；]{2,28}?)(?=[，。;；\n]|$)", conf=0.92, desc="标注-质量标准"),
            # "结算审核质量要求\n误差率<1%"（标签行 + 值行）
            P(r"质量要求\s*[：:]?\s*\n\s*([^\n，。;；]{4,50}?)(?=[。;；\n]|$)",
              0.88, "跨行-质量要求"),
        ],
        preferred_zones=["instructions_front", "notice", "tech_requirements"],
    ),
    # —— 商务风险 ——
    "performance_bond": RulePack(
        field_key="performance_bond",
        patterns=[
            P(r"履约保证金[^\n（）()]{0,8}[（(]\s*([^（）()]{2,30})[）)]", 0.93, "括号-履约保证金"),
            _std(r"履约保证金", conf=0.90, desc="标注-履约保证金"),
        ],
        normalizer=NORM_AMOUNT,
        preferred_zones=["instructions", "contract", "instructions_front"],
    ),
    "deposit_refund": RulePack(
        field_key="deposit_refund",
        patterns=[_std(r"保证金退还|退还保证金|退保",
                       vh=r"([^\n，。;；]{2,50}?)(?=[，。;；\n]|$)", conf=0.88, desc="标注-保证金退还")],
        preferred_zones=["instructions_front", "instructions"],
    ),
    "deposit_method": RulePack(
        field_key="deposit_method",
        patterns=[_std(r"保证金缴纳方式|缴纳方式|递交方式",
                       vh=r"([^\n，。;；]{2,40}?)(?=[，。;；\n]|$)", conf=0.88, desc="标注-缴纳方式")],
        preferred_zones=["instructions_front", "instructions"],
    ),
    "payment_terms": RulePack(
        field_key="payment_terms",
        patterns=[_std(r"付款条件|付款方式|结算方式|支付方式",
                       vh=r"([^\n，。;；]{2,40}?)(?=[，。;；\n]|$)", conf=0.88, desc="标注-付款条件")],
        preferred_zones=["instructions_front", "contract"],
    ),
    "objection_clause": RulePack(
        field_key="objection_clause",
        patterns=[_std(r"异议与投诉|异议处理|提出异议",
                       vh=r"([^\n，。;；]{2,50}?)(?=[，。;；\n]|$)", conf=0.88, desc="标注-异议与投诉")],
        preferred_zones=["instructions", "notice"],
    ),
    # —— 投标准备 ——
    "doc_get_way": RulePack(
        field_key="doc_get_way",
        patterns=[_std(r"招标文件获取方式|获取招标文件方式|招标文件的?获取",
                       vh=r"([^\n，。;；]{2,50}?)(?=[，。;；\n]|$)", conf=0.88, desc="标注-获取方式")],
        preferred_zones=["notice", "instructions_front"],
    ),
    "doc_price": RulePack(
        field_key="doc_price",
        patterns=[
            P(r"(?:招标文件|文件)售价[^\n（）()]{0,6}[（(]\s*([^（）()]{1,20})[）)]", 0.93, "括号-文件售价"),
            _std(r"招标文件售价|文件售价|工本费", conf=0.90, desc="标注-文件售价"),
        ],
        normalizer=NORM_AMOUNT,
        preferred_zones=["notice", "instructions_front"],
    ),
    "submission_location": RulePack(
        field_key="submission_location",
        patterns=[_std(r"投标文件递交地点|递交地点|投标文件提交地点|提交地点",
                       conf=0.90, desc="标注-递交地点")],
        preferred_zones=["instructions_front", "notice", "instructions"],
    ),
    # —— 服务要求 ——
    "warranty_period": RulePack(
        field_key="warranty_period",
        patterns=[_std(r"质保期|保修期|质量保证期|缺陷责任期",
                       vh=r"([^\n，。;；]{1,20}?)(?=[，。;；\n]|$)", conf=0.90, desc="标注-质保期")],
        normalizer=NORM_DURATION,
        preferred_zones=["instructions_front", "tech_requirements", "notice"],
    ),
    "acceptance_requirements": RulePack(
        field_key="acceptance_requirements",
        patterns=[_std(r"验收要求|验收标准",
                       vh=r"([^\n，。;；]{2,40}?)(?=[，。;；\n]|$)", conf=0.88, desc="标注-验收要求")],
        preferred_zones=["instructions_front", "tech_requirements"],
    ),
    # —— 评标总纲 ——
    "evaluation_method": RulePack(
        field_key="evaluation_method",
        patterns=[_std(r"评标办法|评审办法|评标方式",
                       vh=r"([^\n，。;；]{2,30}?)(?=[，。;；\n]|$)", conf=0.92, desc="标注-评标办法")],
        preferred_zones=["evaluation_method", "instructions_front"],
    ),
    "evaluation_committee": RulePack(
        field_key="evaluation_committee",
        patterns=[_std(r"评标委员会(?:的)?组成|评标委员会构成",
                       vh=r"([^\n，。;；]{2,50}?)(?=[，。;；\n]|$)", conf=0.88, desc="标注-评标委员会")],
        preferred_zones=["evaluation_method"],
    ),
    "technical_weight": RulePack(
        field_key="technical_weight",
        patterns=[P(r"技术(?:标|部分)?[^\d%。]{0,8}(\d{1,3})\s*分", 0.90, "技术标分值")],
        preferred_zones=["evaluation_method", "scoring"],
    ),
    "commercial_weight": RulePack(
        field_key="commercial_weight",
        patterns=[P(r"商务(?:标|部分)?[^\d%。]{0,8}(\d{1,3})\s*分", 0.90, "商务标分值")],
        preferred_zones=["evaluation_method", "scoring"],
    ),
    "price_weight": RulePack(
        field_key="price_weight",
        patterns=[P(r"(?:价格|报价)(?:标|部分)?[^\d%。]{0,8}(\d{1,3})\s*分", 0.90, "价格标分值")],
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


_NBSP = chr(160)
_OCR_SPACE = re.compile(r"(\d)[  ]+(\d)|(?<=[A-Za-z0-9\-])[  ]+(?=[A-Za-z0-9\-])")


_OCR_SPACE_SUB = "\\1\\2"  # re.sub 分组引用


def _squeeze_ocr_spaces(text: str) -> str:
    """合并 OCR 在字母/数字之间插入的空格：'S C A N - 2 0 2 6'→'SCAN-2026'，'1 2月'→'12月'。"""
    prev = None
    while prev != text:
        prev = text
        text = _OCR_SPACE.sub(_OCR_SPACE_SUB, text)
    return text


def rule_hit(pack: RulePack, text: str):
    """对一段文本执行规则包 → (raw_value, normalized, span) | None。

    normalized 恒为标量：amount→float(元)、datetime→ISO str、duration→'540日历天'、text→str。
    返回第 4 元为模式置信度（分层模式库）。
    """
    if pack.ocr_space_fix:
        text = _squeeze_ocr_spaces(text)
    for pat_def in pack.patterns:
        m = pat_def.regex.search(text)
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
        return raw, norm_value, m.span(1), pat_def.conf
    return None


_INVALID_VALUES = {"无", "见", "详见", "/", "前附表", "详见第五章", "详见下表", "详见附件"}
