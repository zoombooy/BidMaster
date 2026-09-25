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

31 个用例覆盖：中文大写金额/日期/工期归一化、编号归一化与章节树、锚区定位、评分表逐行解析与分值校验、跨页续表合并、端到端管线（含缓存续跑）。

## 设计来源与致谢

本项目的机制设计吸收了以下开源项目的经验（详见方案文档）：

- [FB208/OpenBidKit_Yibiao](https://github.com/FB208/OpenBidKit_Yibiao)——18 项解析并发/prompt 缓存预热、Schema 校验+错误回灌、checkpoint/定向重跑/级联失效
- [Inupedia/tender-extract](https://github.com/Inupedia/tender-extract)——规则优先 + LLM 兜底、字段级证据链、F1 评测门禁
- [fanfanyuyang/bid-agent-vscode](https://github.com/fanfanyuyang/bid-agent-vscode)——处理账本、字段四态、跨页续表合并
- [Router0824/BidPilot-AI](https://github.com/Router0824/BidPilot-AI)——分值级要求模型（分值/置信度/页码/原文）
- [guangshu100/BidMaster-Pro](https://github.com/guangshu100/BidMaster-Pro)——人工审核闸门思路
- [Get00/BiaoShu-SKILL](https://github.com/Get00/BiaoShu-SKILL)——"评分标准 JSON 化"数据流
- 解析底座：[MinerU](https://github.com/opendatalab/MinerU) / [PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR)

代码为独立实现（未拷贝上述项目源码），各灵感来源的 License 见 THIRD_PARTY_NOTICES.md。

## 路线图

- **一期（本仓库）**：读标——解析 + 字段 + 评分标准结构化 ✅
- **二期**：企业业绩库/人员库/证书库自动匹配（保守判定）+ 评分镜像目录驱动的标书大纲生成 + 废标自查门禁
- **三期**：审阅工作台前端（bbox 高亮定位）、PostgreSQL/pgvector 存储层、LangGraph 编排升级

## License

MIT
