# BidMaster —— 智能标书 Agent（一期：读标）

把招标文件变成**结构化、可溯源、可信**的数据资产。一期聚焦"读标"：

1. **招标文件解析**——Word(.docx)/PDF 文本层/扫描件三链路；章节树与锚区定位；表格结构化（跨页续表合并）；处理账本保证无静默遗漏
2. **关键字段抽取**——项目名称/编号/时间/限价/工期/保证金/评标办法等 17 个字段；规则优先 + LLM 兜底；多来源冲突检测
3. **评分标准结构化**——评分表逐行映射为评分项（技术/商务/价格）；四类技术要求建模（类似业绩/项目负责人/人员配备/工作方案含暗标）；商务六类；★实质性条款；分值合计机械校验
4. **全文证据链**——每条结果带页码/坐标/原文片段，无证据不采纳

完整设计文档见仓库外《智能标书Agent技术方案》或 [方案要点](#设计来源)。

## 快速开始

```bash
# 1) 环境（任选其一）
uv venv --python 3.11 && uv pip install -r requirements.txt
# 或: python -m venv .venv && .venv/Scripts/pip install -r requirements.txt

# 2) 跑通示例（生成示例招标文件并解析）
python scripts/make_sample.py
python -m bidmaster analyze evals/golden/sample_tender.docx

# 3) 解析你自己的招标文件
python -m bidmaster analyze <你的招标文件.docx|pdf> [--force] [--no-llm] [--json]

# 4) 启动 API
python -m bidmaster serve --port 8000
#   POST /api/analyze (multipart file) / GET /api/reports/{doc_id}
```

## 配置（全部可选）

| 环境变量 | 说明 |
|---------|------|
| `BIDMASTER_LLM_BASE_URL` / `BIDMASTER_LLM_API_KEY` / `BIDMASTER_LLM_MODEL` | OpenAI 兼容端点（DeepSeek/GLM/Qwen/Ollama 均可）。**不配置即纯规则模式，字段抽取照常工作** |
| `BIDMASTER_OCR_BACKEND` | 扫描件 OCR：`mineru`（pip install mineru）或 `paddle`。未配置时扫描页如实记 FAILED_REVIEW |
| `BIDMASTER_REVIEW_THRESHOLD` | 置信度低于阈值进人工复核队列（默认 0.7） |

## A2A 协议：让其他 Agent 调用

BidMaster 实现了 **A2A（Agent2Agent）协议**（protocolVersion 0.2.x），任何 Agent
平台（LangChain / CrewAI / Coze / Dify / Claude / Gemini 等）都可以发现并调用它。

### 1. 能力发现（Agent Card）

```bash
curl http://127.0.0.1:8000/.well-known/agent.json
```

返回 Agent Card：技能 `tender_analysis`（招标文件解析与评分标准结构化）、输入输出
模式、流式/推送能力声明。

### 2. 发起任务（JSON-RPC，异步任务模型）

```bash
# 用文件（base64）发起解析任务
curl -X POST http://127.0.0.1:8000/a2a -H "Content-Type: application/json" -d '{
  "jsonrpc": "2.0", "id": 1, "method": "message/send",
  "params": {"message": {"role": "user", "kind": "message", "messageId": "m1",
    "parts": [{"kind": "file", "file": {
      "name": "tender.pdf",
      "mimeType": "application/pdf",
      "bytes": "<base64内容>"}}]}}}'

# → 返回 Task{id, status:{state:"submitted"}}，后台执行
```

也支持 `file.uri`（HTTP 地址自动下载）与 `DataPart{file_base64, file_name}`。

### 3. 轮询结果 / 流式订阅 / 推送回调

```bash
# 轮询
curl -X POST http://127.0.0.1:8000/a2a -d '{"jsonrpc":"2.0","id":2,
  "method":"tasks/get","params":{"id":"<task_id>"}}'
# → completed 后 artifacts[0].parts[0].data 即完整解析报告（JSON）
```

- **`message/stream`**：SSE 流式订阅，依次推送 `status-update`（working）→
  `artifact-update`（报告）→ `status-update`（completed, final=true）
- **`tasks/pushNotificationConfig/set`**：注册 webhook，任务终态主动 POST 回调
- **`tasks/cancel`**：取消未完成任务（终态任务返回 -32002）

### 4. 供人类使用的 REST（并存）

`POST /api/analyze`（multipart 上传，同步返回报告）、`GET /api/reports/{doc_id}`、
Swagger 文档 `http://127.0.0.1:8000/docs`。

## 架构

```
接入层  清点(SHA-256)/类型路由/处理账本(DONE|EXCLUDED|FAILED_REVIEW)/完成门
解析层  docx(python-docx) ｜ pdf文本层(PyMuPDF+find_tables) ｜ 扫描件(MinerU/PaddleOCR 可插拔)
        表格专项：跨页续表合并 / 横向合并单元格去重
结构层  章节树(样式+编号归一化) ｜ 三层锚区定位(投标人须知前附表/评标办法/评分细则…)
抽取层  字段四级管线：表格标签 > 锚区规则 > 全文规则 > LLM兜底(证据回查，无证据不采纳)
        字段四态：found / n/a / not_found / failed（不编造）
评分层  评分表→ScoreItem(类别/分值/规则/证明材料) ｜ 四类技术要求建模 + 商务六类
        ★条款抽取 ｜ 分值合计机械校验(评分项合计=声明总分=权重=100)
编排层  确定性状态机 intake→parse→structure→fields→scoring→report
        每阶段 checkpoint 落盘（work/<doc_id>/），--force 级联失效下游
接口层  A2A 协议（Agent Card + JSON-RPC：message/send、tasks/get、
        message/stream SSE、推送回调）→ 任意 Agent 平台可发现可调用
        REST API（人类/传统系统）→ 同一份解析管线
```

## 报告长什么样

```jsonc
{
  "fields": {"price_limit": {"value_normalized": "250000000", "confidence": 0.95,
                              "evidence_ids": ["e-...-00021"], "status": "found"}},
  "scores": [{"score_id": "T-001", "category": "technical", "name": "类似工程业绩",
               "max_score": 6.0, "parsed_rule": {"years": 5, "amount_min": 50000000,
               "unit_score": 2.0}, "evidence_required": ["中标通知书", "合同协议书"]}],
  "requirements": [{"req_id": "R-001", "type": "leader", "subject": "项目负责人",
                     "parsed": {"registered_builder": "...一级注册建造师", "title": "高级工程师"}}],
  "sum_checks": [{"category": "technical", "computed_total": 60.0, "declared_total": 60.0, "ok": true}],
  "issues": ["必填字段[预算金额]未提取到，请人工补录"]
}
```

## 评测

```bash
python evals/evaluate.py work/<doc_id>/06_report.json evals/golden/sample_golden.json --fail-under 0.85
```

字段级 P/R/F1 + 评分项召回率。拿真实招标文件跑分：把文件放入 `evals/golden/`，按 `sample_golden.json` 格式标注真值即可。

## 测试

```bash
python -m pytest tests/ -q
```

39 个用例覆盖：中文大写金额/日期/工期归一化、编号归一化与章节树、锚区定位、评分表逐行解析与分值校验、跨页续表合并、端到端管线（含缓存续跑）、**A2A 协议全流程**（能力发现/message/send/轮询/取消语义/推送配置/协议错误码）。

## 设计来源与致谢

本仓库代码为**独立实现**（未拷贝第三方源码），机制设计吸收了以下开源项目的经验：

- [FB208/OpenBidKit_Yibiao](https://github.com/FB208/OpenBidKit_Yibiao)——18 项解析并发/prompt 缓存预热、Schema 校验+错误回灌、checkpoint/定向重跑/级联失效
- [Inupedia/tender-extract](https://github.com/Inupedia/tender-extract)——规则优先 + LLM 兜底、字段级证据链、F1 评测门禁
- [fanfanyuyang/bid-agent-vscode](https://github.com/fanfanyuyang/bid-agent-vscode)——处理账本、字段四态、跨页续表合并
- [Router0824/BidPilot-AI](https://github.com/Router0824/BidPilot-AI)——分值级要求模型（分值/置信度/页码/原文）
- [guangshu100/BidMaster-Pro](https://github.com/guangshu100/BidMaster-Pro)——人工审核闸门思路
- [Get00/BiaoShu-SKILL](https://github.com/Get00/BiaoShu-SKILL)——"评分标准 JSON 化"数据流
- 解析底座：[MinerU](https://github.com/opendatalab/MinerU) / [PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR)

代码为独立实现（未拷贝上述项目源码），各灵感来源的 License 见 THIRD_PARTY_NOTICES.md。

## 实战能力（经真实国网特高压招标文件验证）

- **整包直读**：`python -m bidmaster analyze 招标文件.zip`——自动递归解压（GBK 文件名修复、
  压缩炸弹防护）、收集全部 docx/pdf/xlsx、合并解析；.doc 等不可解析格式记账转人工不阻塞
- **xlsx 链路**：保证金一览表等附件直接解析（"应提交投标保证金（万元）"自动单位换算）
- **国网评分格式**：四列评分表（分值嵌在"（18分）"文字）、按分标(lot)独立评分表、
  扣分项（-30分）、技:商:价权重矩阵、价格公式矩阵（公式/下浮/正向/负向系数逐分标结构化）
- **分值校验按分标分组**：每个分标内部"评分项合计 == 大类标签合计"（实测 32/33 组通过）
- **梯度量化**："优16-20分，良12-15分" → `grades: [{tier, min, max}]`
- **要求建模**：业绩时间窗（近N年/开标前N年/投标截止日近N年）、证书编号配对
  （参考 tender-extract 模式分类学）、从评分表"项目内容"列直接提取业绩/人员要求

## 路线图

- **一期（本仓库）**：读标——解析 + 字段 + 评分标准结构化 + A2A 对外服务 ✅
- **二期**：企业业绩库/人员库/证书库自动匹配（保守判定）+ 评分镜像目录驱动的标书大纲生成 + 废标自查门禁
- **三期**：审阅工作台前端（bbox 高亮定位）、PostgreSQL/pgvector 存储层、LangGraph 编排升级、MCP Server 形态

## License

MIT
