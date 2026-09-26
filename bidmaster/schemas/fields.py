"""关键字段抽取结果模型：字段四态 + 置信度 + 证据引用。"""
from __future__ import annotations

from typing import Optional
from pydantic import BaseModel, Field

# 字段四态：禁止编造——找不到就如实写 not_found
STATUS_FOUND = "found"            # 明确存在（有值 + 证据）
STATUS_NOT_APPLICABLE = "n/a"     # 不适用（本项目无此概念）
STATUS_NOT_FOUND = "not_found"    # 未发现（文档中找不到）
STATUS_FAILED = "failed"          # 多次尝试后失败，转人工

SOURCE_TABLE = "table"
SOURCE_RULES = "rules"
SOURCE_LLM = "llm"


class FieldCandidate(BaseModel):
    """抽取过程中收集的候选值（用于冲突检测）。"""
    value_raw: str
    value_normalized: Optional[str] = None
    source: str
    confidence: float
    evidence_id: str = ""
    zone: str = ""


class FieldExtraction(BaseModel):
    field_key: str
    field_label: str = ""
    group: str = ""
    required: bool = False
    status: str = STATUS_NOT_FOUND
    value_raw: str = ""
    value_normalized: Optional[str] = None  # 归一化结果（金额→元、日期→ISO、工期→天数）
    value_unit: str = ""  # 元 | 天 | datetime | 文本
    confidence: float = 0.0
    evidence_ids: list[str] = []
    candidates: list[FieldCandidate] = []  # 全部来源候选（含被放弃的）
    note: str = ""


class FieldConflict(BaseModel):
    field_key: str
    chosen: str = ""
    alternatives: list[FieldCandidate] = []
    note: str = ""


# 字段登记表：分组 / 标签 / 是否必填（终检项）
FIELD_CATALOG: dict[str, dict] = {
    "project_name":     {"label": "项目名称",     "group": "项目信息", "required": True},
    "tender_no":        {"label": "招标编号",     "group": "项目信息", "required": True},
    "tenderer":         {"label": "招标人",       "group": "项目信息", "required": False},
    "agency":           {"label": "招标代理机构", "group": "项目信息", "required": False},
    "legal_representative": {"label": "法定代表人", "group": "项目信息", "required": False},
    "contact_phone":    {"label": "联系电话",     "group": "项目信息", "required": False},
    "credit_code":      {"label": "统一社会信用代码", "group": "资格核验", "required": False},
    "fund_source":      {"label": "资金来源",     "group": "项目信息", "required": False},
    "site":             {"label": "建设地点",     "group": "项目信息", "required": False},
    "price_limit":      {"label": "最高限价",     "group": "价格",     "required": True},
    "budget_amount":    {"label": "预算金额",     "group": "价格",     "required": False},
    "security_deposit": {"label": "投标保证金",   "group": "价格",     "required": False},
    "deposit_refund":   {"label": "保证金退还条件", "group": "商务风险", "required": False},
    "deposit_method":   {"label": "保证金缴纳方式", "group": "商务风险", "required": False},
    "performance_bond": {"label": "履约保证金",   "group": "商务风险", "required": False},
    "payment_terms":    {"label": "付款条件",     "group": "商务风险", "required": False},
    "objection_clause": {"label": "异议与投诉",   "group": "商务风险", "required": False},
    "bid_deadline":     {"label": "投标截止/开标时间", "group": "时间", "required": True},
    "bid_validity":     {"label": "投标有效期",   "group": "时间",     "required": False},
    "duration":         {"label": "计划工期/服务期", "group": "工期质量", "required": True},
    "quality_standard": {"label": "质量标准",     "group": "工期质量", "required": False},
    "warranty_period":  {"label": "质保期",       "group": "服务要求", "required": False},
    "acceptance_requirements": {"label": "验收要求", "group": "服务要求", "required": False},
    "doc_get_way":      {"label": "招标文件获取方式", "group": "投标准备", "required": False},
    "doc_price":        {"label": "招标文件售价", "group": "投标准备", "required": False},
    "submission_location": {"label": "投标文件递交地点", "group": "投标准备", "required": False},
    "evaluation_method": {"label": "评标办法",    "group": "评标总纲", "required": False},
    "evaluation_committee": {"label": "评标委员会", "group": "评标总纲", "required": False},
    "technical_weight": {"label": "技术标分值",   "group": "评标总纲", "required": False},
    "commercial_weight": {"label": "商务标分值", "group": "评标总纲", "required": False},
    "price_weight":     {"label": "价格标分值",   "group": "评标总纲", "required": False},
}


def empty_extraction(field_key: str) -> FieldExtraction:
    meta = FIELD_CATALOG.get(field_key, {})
    return FieldExtraction(
        field_key=field_key, field_label=meta.get("label", field_key),
        group=meta.get("group", ""), required=meta.get("required", False),
    )
