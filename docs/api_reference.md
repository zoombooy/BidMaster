# BidMaster 招标文件解析 Agent 接口文档

> 版本：0.1.0 ｜ 更新：2026-09-28
> 基地址：`http://172.19.136.137:8200`

---

## 概述

BidMaster 提供四种接入方式供业务系统调用：

| 协议 | 基地址 | 适用场景 | 同步/异步 |
|------|--------|---------|-----------|
| **Web UI** | `http://172.19.136.137:8200/ui` | 人工上传文件、查看报告、点击字段值看原文证据（PDF 带 bbox 高亮） | 浏览器 |
| REST | `http://172.19.136.137:8200/api` | 上传文件→同步返回报告；查询已有报告 | 同步 |
| MCP | `http://172.19.136.137:8200/mcp` | Agent 平台（Yuxi/LangChain/Coze）按 MCP 协议调用工具 | 同步 |
| A2A | `http://172.19.136.137:8200/a2a` | 异步任务模型（提交→轮询/流式/推送回调） | 异步 |

三协议底层共用同一条解析管线，返回数据结构一致。

---

## 0. Web UI

浏览器打开 `http://172.19.136.137:8200/ui`，零插件、零依赖（单页应用由 FastAPI 直接托管）。

功能（交互模式参考 MinerU 上传即解析 / RAGFlow 引用高亮）：

- **上传**：拖拽或点击选择 .docx/.doc/.pdf，带上传进度与解析状态；勾选 `force` 强制重跑
- **历史报告**：左侧栏自动加载最近 100 份报告（`GET /api/reports`），点击即查
- **报告查看**：字段提取（按分组、状态标签、置信度条、必填标记）｜包件字段（多包切换）｜评分标准（技术/商务/价格分类、分值合计）｜商务要求｜否决条款｜校验与问题｜章节结构
- **证据回溯**：点击任意字段值/评分项"证据"链接 → 右侧抽屉展示原文片段；PDF 来源自动渲染原文页图并按 bbox 画高亮框（当前证据蓝框、同页其他证据绿框）；docx 来源显示文本片段（docx 无页码属预期）
- **导出**：一键下载报告 JSON、复制 doc_id

对应支撑接口：`GET /api/reports`（列表）、`GET /api/reports/{doc_id}/evidence/{eid}/page.png`（页图，响应头 `X-Page-Rect` 页面点尺寸 + `X-Bbox` 证据坐标）。

## 1. REST API

### 1.1 上传并解析

```
POST /api/analyze
Content-Type: multipart/form-data
```

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `file` | file | 是 | 招标文件（.docx / .pdf / .xlsx / .zip 整包） |
| `force` | boolean | 否 | `true` = 忽略缓存强制重跑。同一文件（按内容 SHA-256 识别）重复解析默认走缓存秒回；改代码/想看实时耗时解析时传 `true` |

**同步返回**：完整解析报告 JSON（挂起解析，最长 10-60 秒取决于文件大小和分标数量）。

**curl 示例**：

```bash
curl -X POST http://172.19.136.137:8200/api/analyze \
  -F "file=@招标文件.docx"

# 强制重跑（不走缓存）
curl -X POST http://172.19.136.137:8200/api/analyze \
  -F "file=@招标文件.docx" \
  -F "force=true"
```

**返回结构**：

```json
{
  "doc_id": "83d13390f1b1",
  "file_name": "第一章招标公告SG2682.docx + …（多文件合并）",
  "fields": {
    "project_name": {
      "status": "found",
      "value_raw": "陕西-河南±800千伏直流输电工程",
      "value_normalized": "陕西-河南±800千伏直流输电工程",
      "confidence": 0.95,
      "evidence_ids": ["e--00004"],
      "note": ""
    },
    "tender_no": {
      "status": "found",
      "value_normalized": "0711-26OTL11013056",
      "confidence": 0.95
    },
    "price_limit": {
      "status": "found",
      "value_normalized": "18746000",
      "value_unit": "元",
      "confidence": 0.95
    },
    "legal_representative": {
      "status": "not_found",
      "note": "文档中未发现该字段（检索过表格标签、锚区与全文）",
      "confidence": 0
    }
  },
  "conflicts": [
    {
      "field_key": "price_limit",
      "chosen": "18746000",
      "alternatives": [...],
      "note": "数值差异过大（最高 1.8746e+07 / 最低 1），可能为不同分标/包件的数据"
    }
  ],
  "scores": [
    {
      "score_id": "T-001",
      "category": "technical",
      "name": "±800kV及以上换流站项目",
      "max_score": 60.0,
      "rule_type": "graded",
      "rule_text": "近5年每提供1项…",
      "parsed_rule": {
        "years": 5, "amount_min": 50000000,
        "unit_score": 2.0, "max_score_cap": 6.0,
        "scope": {"voltage_levels": ["800KV"], "facilities": ["换流站"]},
        "lot": "换流站土建施工A包", "group": "技术水平"
      },
      "evidence_required": ["中标通知书", "合同协议书", "竣工验收证明"],
      "evidence_ids": ["e--00042"],
      "confidence": 0.9,
      "status": "pending_review"
    }
  ],
  "requirements": [
    {
      "req_id": "R-001",
      "type": "performance",
      "subject": "投标人",
      "constraint": "近5年每提供1项单项合同金额5000万元以上的同类房屋建筑工程业绩得2分…",
      "parsed": {"years": 5, "count_min": 1, "amount_min": 50000000, "unit_score": 2.0},
      "scoring_refs": ["T-001"],
      "evidence_ids": ["e--00043"],
      "confidence": 0.85
    }
  ],
  "star_clauses": [...],
  "rejections": [
    {"rej_id": "RJ-001", "category": "主体不符", "text": "（1）为招标人不具备独立法人资格…", "severity": "high"}
  ],
  "sum_checks": [
    {"category": "technical", "declared_total": 50.0, "computed_total": 50.0,
     "item_count": 12, "state": "passed", "ok": true, "lot": "换流站土建施工A包"}
  ],
  "evidence": [
    {"evidence_id": "e--00001", "source_file": "第一章招标公告SG2682.docx",
     "kind": "table_cell", "page_no": 0, "bbox": null,
     "table_id": "t-0030", "row": 1, "snippet": "工程名称 → 陕西-河南±800千伏直流输电工程"}
  ],
  "lot_fields": {
    "结算审核包1": {
      "price_limit": {"status": "found", "value_normalized": "5810000"},
      "duration": {"status": "found", "value_normalized": "2026年11月至2030年4月"}
    },
    "结算审核包2": {"price_limit": {"status": "found", "value_normalized": "5170000"}}
  },
  "lot_info": [
    {"lot": "结算审核包1", "project": "浙江特高压交流环网线路工程", "source_file": "…"}
  ],
  "issues": ["必填字段[计划工期/服务期]未提取到，请人工补录"],
  "stats": {
    "fields_found": 20, "fields_total": 31,
    "score_items": 133, "requirements": 24,
    "star_clauses": 2, "lots": 33,
    "llm": "on", "gate": "pending_review"
  },
  "quality": {"ocr_used": false, "warnings": []},
  "ledger": {"total": 3, "done": 2, "failed_review": 1, "complete_gate_passed": true}
}
```

**关键字段说明**：

| 字段路径 | 含义 |
|---------|------|
| `doc_id` | 唯一标识，后续查询/获取证据用 |
| `fields.{key}.status` | `found`=有值 / `n.a.`=不适用 / `not_found`=未发现 / `failed`=失败 |
| `fields.{key}.value_normalized` | 归一化后的值（金额→元、日期→ISO、工期→天） |
| `fields.{key}.confidence` | 0-1 浮点数；<0.7 建议人工复核 |
| `fields.{key}.evidence_ids` | 证据 ID 列表，可在 `evidence` 数组中查找原文 |
| `scores[].category` | `technical` 技术标 / `commercial` 商务标 / `price` 价格标 |
| `scores[].parsed_rule.lot` | 所属分标名（多分标招标时有值） |
| `scores[].parsed_rule.penalty` | `true` 表示扣分项（负分） |
| `scores[].parsed_rule.grades` | 梯度量化 `[{"tier":"优","min":16,"max":20}]` |
| `requirements[].type` | `performance` 业绩 / `leader` 负责人 / `team` 人员 / `plan` 方案 / `qualification` 资质 / `finance` 财务 / `credit` 信用 / `honor` 荣誉 / `commitment` 承诺 / `price` 价格公式 |
| `requirements[].parsed` | 结构化要素（years/amount_min/count_min/certificates/count/scope…） |
| `requirements[].scoring_refs` | 关联的评分项 ID（如 `["T-001"]`） |
| `sum_checks[].state` | `passed` / `failed` / `unverifiable`（无声明总分无法校验） |
| `issues` | 需人工关注的提示列表（漏提/低置信/冲突） |
| `stats.gate` | `pending_review`（默认）——正式使用前需人工审阅 |

---

### 1.2 查询已有报告

```
GET /api/reports/{doc_id}
```

返回与 1.1 相同结构的完整 JSON 报告。

```bash
curl http://172.19.136.137:8200/api/reports/83d13390f1b1
```

### 1.3 证据原文页渲染

```
GET /api/reports/{doc_id}/evidence/{evidence_id}/page.png
```

返回证据所在页面的 PNG 截图。仅 PDF 来源的证据有页码（docx 链路页码为 0 时返回 404）。响应头 `X-Source-File` 为来源文件名、`X-Bbox` 为坐标。

---

## 2. MCP 协议（Agent 平台集成）

### 2.1 端点

```
POST http://172.19.136.137:8200/mcp
Content-Type: application/json
```

MCP 协议版本 `2024-11-05`，使用 JSON-RPC 2.0。

### 2.2 工具列表

| 工具名 | 说明 | 关键参数 |
|--------|------|---------|
| `analyze_tender` | 解析招标文件（文件路径/URL/base64），返回结构化摘要 | `local_path` / `file_url` / `file_base64` / `use_llm` / `force` |
| `get_report` | 按 doc_id 获取完整报告，支持过滤 | `doc_id`（必填）/ `lot` / `category` / `section` / `page` / `page_size` |
| `list_lots` | 列出分标清单（名称/评分项数/合计分值） | `local_path` 或 `doc_id` |

### 2.3 调用示例

**初始化握手**（首次调用前必须）：

```json
{"jsonrpc": "2.0", "id": 1, "method": "initialize",
 "params": {"protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "your-system", "version": "1.0"}}}
```

**列出工具**：

```json
{"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
```

**调用解析**：

```json
{"jsonrpc": "2.0", "id": 3, "method": "tools/call",
 "params": {"name": "analyze_tender",
            "arguments": {"local_path": "/path/to/tender.docx"}}}
```

**按分标过滤报告**：

```json
{"jsonrpc": "2.0", "id": 4, "method": "tools/call",
 "params": {"name": "get_report",
            "arguments": {"doc_id": "83d13390f1b1", "lot": "结算审核包1", "section": "scores"}}}
```

**Python 客户端示例（langchain-mcp-adapters）**：

```python
import asyncio
from langchain_mcp_adapters.client import MultiServerMCPClient

async def main():
    client = MultiServerMCPClient({
        "bidmaster": {"url": "http://172.19.136.137:8200/mcp",
                       "transport": "streamable_http"}})
    tools = await client.get_tools()
    # tools[0] = analyze_tender, tools[1] = get_report, tools[2] = list_lots
    result = await tools[0].ainvoke({"local_path": "/path/to/tender.docx"})
    print(result)

asyncio.run(main())
```

---

## 3. A2A 协议（异步任务模型）

### 3.1 提交任务

```
POST http://172.19.136.137:8200/a2a
Content-Type: application/json
```

```json
{
  "jsonrpc": "2.0", "id": 1,
  "method": "message/send",
  "params": {
    "message": {
      "role": "user", "kind": "message", "messageId": "msg-001",
      "parts": [
        {"kind": "file", "file": {
          "name": "tender.docx",
          "mimeType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
          "bytes": "<base64 内容>"
        }}
      ]
    }
  }
}
```

**返回**：

```json
{
  "jsonrpc": "2.0", "id": 1,
  "result": {
    "id": "task-abc123", "contextId": "ctx-456",
    "status": {"state": "submitted", "timestamp": "2026-09-28T12:00:00Z"},
    "kind": "task"
  }
}
```

### 3.2 轮询任务

```
POST /a2a
{"jsonrpc": "2.0", "id": 2, "method": "tasks/get", "params": {"id": "task-abc123"}}
```

状态流转：`submitted → working → completed | failed`

`completed` 后 `artifacts[0].parts[0].data` 即为完整解析报告（结构同 1.1）。

### 3.3 流式订阅（SSE）

```
POST /a2a
{"jsonrpc": "2.0", "id": 3, "method": "message/stream", "params": {...同 message/send...}}
```

响应为 `text/event-stream`，事件顺序：

```
data: {"result": {..., "status": {"state": "working"}}}
data: {"result": {"kind": "artifact-update", "artifact": {"parts": [...]}}}
data: {"result": {"kind": "status-update", "status": {"state": "completed"}, "final": true}}
```

### 3.4 推送回调（Webhook）

```
POST /a2a
{"jsonrpc": "2.0", "id": 4, "method": "tasks/pushNotificationConfig/set",
 "params": {"taskId": "task-abc123", "pushNotificationConfig": {"url": "https://your-system.com/callback"}}}
```

任务终态时向 `url` 发送 POST（body 为完整 Task JSON），带 `X-A2A-Notification-Token` 头（如配置了 token）。

### 3.5 取消任务

```
POST /a2a
{"jsonrpc": "2.0", "id": 5, "method": "tasks/cancel", "params": {"id": "task-abc123"}}
```

---

## 4. 错误码

| HTTP 状态码 | JSON-RPC 错误码 | 说明 |
|------------|----------------|------|
| 400 | -32700 | JSON 解析失败 |
| 400 | -32602 | 参数不合法 |
| 400 | -32601 | 方法不存在 |
| 404 | -32001 | 任务/报告不存在 |
| 405 | -32002 | 任务不可取消（已完成/已失败） |
| 413 | — | 上传文件超大小限制（200MB） |
| 422 | — | 文件格式不支持或内容不可解析 |

---

## 5. 解析报告 JSON 结构完整说明

```
TenderReport
├── doc_id: str                        # 唯一标识
├── file_name: str                     # 文件名（多文件合并时为 "A + B + C"）
├── generated_at: str                  # ISO 时间
├── fields: dict[str, FieldExtraction]  # 31 个关键字段
│   └── {key: {
│         status: found|n.a.|not_found|failed
│         value_raw: str               # 原文值
│         value_normalized: str        # 归一化值（金额→元、日期→ISO、工期→天）
│         confidence: float            # 0-1，<0.7 建议人工复核
│         evidence_ids: [str]          # 证据 ID，可反查原文
│         note: str                    # 补充说明
│       }}
├── conflicts: [FieldConflict]          # 多来源冲突（含全部候选和取值理由）
├── scores: [ScoreItem]                # 评分项列表
│   └── {
│       score_id: "T-001"              # 技术T/商务C/价格P + 序号
│       category: technical|commercial|price
│       name: str                      # 评分项名称
│       max_score: float               # 最高分值
│       rule_type: graded|binary|formula
│       rule_text: str                 # 评分标准原文
│       parsed_rule: {                 # 结构化要素
│         years, amount_min, count_min, unit_score, max_score_cap,
│         scope: {voltage_levels, facilities, work_types, subdivision},
│         lot: str                     # 所属分标
│         group: str                   # 大类分组（如"技术水平"）
│         penalty: bool                # 是否扣分项
│         grades: [{tier, min, max}]   # 梯度量化
│       }
│       evidence_required: [str]       # 证明材料清单
│       evidence_ids: [str]            # 证据 ID
│       confidence: float
│       status: confirmed|pending_review|failed
│     }
├── requirements: [RequirementItem]     # 要求条目（10 种类型）
│   └── {
│       req_id: "R-001"
│       type: performance|leader|team|plan|qualification|finance|credit|honor|commitment|price
│       subject: str                    # 主体（如"项目负责人"/"技术负责人"）
│       constraint: str                 # 要求原文
│       parsed: {years, amount_min, count_min, certificates, position, count, ...}
│       scoring_refs: [score_id]        # 关联评分项
│       evidence_ids: [str]
│       confidence: float
│     }
├── star_clauses: [StarClause]          # ★实质性条款
├── rejections: [RejectionItem]         # 废标/否决风险
│   └── {rej_id, kind: rejection|invalid, origin: explicit|experienced,
│        category: 主体不符|资格不符|…, text: 原文, severity: high|medium|low}
├── sum_checks: [ScoreSumCheck]         # 分值校验（按分标分组）
│   └── {category, declared_total, computed_total, item_count,
│        state: passed|failed|unverifiable, ok, lot, note}
├── issues: [str]                       # 需人工关注的提示
├── evidence: [Evidence]                # 全量证据链
│   └── {evidence_id, source_file, kind, page_no, bbox, char_start,
│        char_end, block_id, table_id, row, col, snippet, source}
├── lot_fields: {lot: {field: FieldExtraction}}  # 分标级字段
├── lot_info: [{lot, project, source_file}]      # 分标与源文件关联
├── issues: [str]                       # 问题清单
└── stats: {fields_found, fields_total, score_items, requirements,
            star_clauses, lots, evidence_count, llm, gate}
```
