"""评分标准结构化模型：评分项（ScoreItem）+ 要求条目（RequirementItem）。

一期把评分标准拆成两层：
  1) ScoreItem —— 评分表逐行映射出的"评分项"（评委打分的最小单元）
  2) RequirementItem —— 从评分项/资格条款进一步拆出的"可核验要求"
     （业绩 / 项目负责人 / 人员配备 / 工作方案 / 商务六类），用于二期"写标"时逐条响应。
"""
from __future__ import annotations

from typing import Optional
from pydantic import BaseModel, Field

# 评分三大类
CAT_TECHNICAL = "technical"
CAT_COMMERCIAL = "commercial"
CAT_PRICE = "price"

# 规则类型
RULE_GRADED = "graded"    # 梯度打分（优/良/一般）
RULE_BINARY = "binary"    # 达标制（有得分，没有不得分）
RULE_FORMULA = "formula"  # 公式（价格分）

# 要求条目类型
REQ_PERFORMANCE = "performance"    # 类似工程业绩
REQ_LEADER = "leader"              # 项目负责人
REQ_TEAM = "team"                  # 工作组人员配备
REQ_PLAN = "plan"                  # 工作方案（含暗标格式）
REQ_QUALIFICATION = "qualification"  # 资质认证
REQ_FINANCE = "finance"            # 财务能力
REQ_CREDIT = "credit"              # 信用记录
REQ_HONOR = "honor"                # 奖项荣誉
REQ_COMMITMENT = "commitment"      # 服务承诺
REQ_PRICE = "price"                # 价格分公式
REQ_STAR_CLAUSE = "star_clause"    # ★号实质性条款


class ScoreItem(BaseModel):
    score_id: str                       # T-001 / C-001 / P-001
    category: str                       # technical | commercial | price
    name: str
    max_score: float = 0.0
    rule_type: str = RULE_BINARY
    rule_text: str = ""                 # 评分标准原文
    parsed_rule: dict = {}              # 结构化解析出的数值约束（数量/金额/年限等）
    formula: Optional[str] = None       # 价格分公式原文
    evidence_required: list[str] = []   # 证明材料要求
    requirement_refs: list[str] = []    # 关联 RequirementItem.req_id
    evidence_ids: list[str] = []
    confidence: float = 0.0
    status: str = "confirmed"           # confirmed | pending_review | failed


class RequirementItem(BaseModel):
    req_id: str                         # R-001
    type: str                           # REQ_* 常量
    subject: str = ""                   # 主体（如"项目负责人"/"技术负责人"）
    constraint: str = ""                # 要求原文
    parsed: dict = {}                   # 结构化字段（数量/金额/年限/证书…）
    scoring_refs: list[str] = []        # 关联 ScoreItem.score_id
    evidence_ids: list[str] = []
    confidence: float = 0.0
    status: str = "confirmed"


class ScoreSumCheck(BaseModel):
    category: str
    declared_total: Optional[float] = None   # 文件明示的类别总分
    computed_total: float = 0.0              # 评分项合计
    item_count: int = 0
    ok: bool = True
    note: str = ""


class StarClause(BaseModel):
    clause_id: str
    text: str = ""
    page_no: int = 0
    evidence_ids: list[str] = []
