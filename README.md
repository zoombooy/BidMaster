<div align="center">

# BidMaster

**招标文件智能解析 Agent**

把招标文件变成结构化、可溯源、可直接驱动标书编写的数据资产

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.11%2B-blue.svg)](https://www.python.org/)
[![Tests](https://img.shields.io/badge/Tests-89%20passing-brightgreen.svg)](#本地验证)
[![A2A](https://img.shields.io/badge/Protocol-A2A%20%7C%20MCP-orange.svg)](#-作为-agent-接入)

</div>

---

## 简介

BidMaster 是一个面向招投标场景的文档解析 Agent，聚焦三件事：

| # | 解析任务 | 输出 |
|---|---------|------|
| **1** | **招标文件解析** | 项目名称、编号、时间、限价、工期、保证金等关键字段；目录章节树；全量表格结构化 |
| **2** | **技术评分标准解析** | 技术评分项（分值/细则/证明材料）；类似工程业绩、项目负责人、人员配备、工作方案四类要求条目 |
| **3** | **商务评分标准解析** | 商务评分项（资质/财务/信用/荣誉）；价格评分公式（含系数）；★实质性条款与废标风险清单 |

三大任务共享同一条解析管线与证据链——每一条抽取结果都回指原文（页码 + 坐标 + 原文片段），**无证据不采纳，缺失如实标注，绝不编造**。

## ✨ 核心特性

**解析**

- Word（.docx）/ PDF 文本层 / 扫描件 OCR（在线 MinerU，表格行列结构化还原）/ xlsx / zip 整包递归解压
- 表格专项：跨页续表合并、合并单元格归一、四列评分表、权重矩阵表
- 章节树 + 编号归一化（第X章 / 1.1 / （一））+ 关键词锚区定位

**抽取**

- 31 个关键字段四级管线：表格标签 → 锚区规则 → 全文规则 → LLM 兜底（原文引用回查）
- 中文大写金额 / 中文日期 / 工期自动归一化；字段四态（found / n.a. / not_found / failed）
- 多来源冲突检测与数值语义提示（如"疑似多分标数据"）

**评分标准结构化**

- 经典表格 + 四列表双格式；按分标（lot）独立建模，含扣分项识别
- 价格公式矩阵（公式 / 下浮系数 / 正向 / 负向系数逐分标抽取）
- 分值机械校验：大类合计 = 子项合计 = 权重合计，不一致即报告

**质量纪律**

- 处理账本 + 完成门：每个页面对象必须有终态，无静默遗漏
- 置信度分级 + 人工复核队列；评测数据飞轮（人工修正 → 金标回流 → CI 门禁）

## 🚀 快速开始

```bash
# 安装
pip install -r requirements.txt

# 解析一份招标文件（不配置 LLM 即为纯规则模式，可直接运行）
python -m bidmaster analyze 招标文件.docx

# 整包 zip 自动递归解压（含嵌套 zip、GBK 文件名修复）
python -m bidmaster analyze 完整招标文件.zip

# 启动 API 服务
python -m bidmaster serve --port 8000
```

## ⚙️ 配置（全部可选）

| 环境变量 | 说明 |
|---------|------|
| `BIDMASTER_LLM_BASE_URL` / `_API_KEY` / `_MODEL` | OpenAI 兼容端点（Qwen / DeepSeek / GLM 等）。未配置时自动运行纯规则模式 |
| `BIDMASTER_OCR_BACKEND` | 扫描件 OCR：`mineru`（在线 API）或 `paddle`；未配置时扫描页如实转人工 |
| `BIDMASTER_ALLOWED_PATHS` | MCP/A2A 接口允许读取的目录白名单 |
| `BIDMASTER_REVIEW_THRESHOLD` | 低置信度人工复核阈值（默认 0.7） |

## 🤖 作为 Agent 接入

**MCP**（streamable_http，供 Yuxi / LangChain / Coze 等平台集成）：

```
POST /mcp            → tools: analyze_tender · get_report · list_lots
GET  /.well-known/agent.json
```

**A2A**（Agent2Agent 协议，异步任务模型）：

```
GET  /.well-known/agent.json      → 能力发现
POST /a2a                          → message/send · tasks/get · message/stream · 推送回调
```

**REST**（面向人类与常规系统）：`POST /api/analyze` · `GET /api/reports/{doc_id}` · Swagger 文档

## 🏗️ 架构

```
接入层  清点/类型路由/处理账本（无静默遗漏）/版本管理
解析层  docx ｜ pdf 文本层 ｜ 扫描件 OCR ｜ xlsx ｜ zip 递归解压 ｜ 表格专项
结构层  章节树 + 编号归一化 + 关键词锚区定位
抽取层  四级字段管线（表格标签→锚区规则→全文规则→LLM 兜底）
        归一化 / 字段四态 / 冲突检测 / 证据溯源（页码+bbox+原文）
评分层  评分表解析（经典+格式）/ 要求建模 / 废标清单 / 分值机械校验
编排层  确定性状态机 + 每阶段 checkpoint + 级联失效
接口层  MCP（A2A 协议）+ REST API
```

## 🙏 致谢

机制设计参考以下开源项目（代码为独立实现，致谢与许可详见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)）：

[OpenBidKit_Yibiao](https://github.com/FB208/OpenBidKit_Yibiao) ·
[tender-extract](https://github.com/Inupedia/tender-extract) ·
[BidPilot-AI](https://github.com/Router0824/BidPilot-AI) ·
[bid-agent-vscode](https://github.com/fanfanyuyang/bid-agent-vscode) ·
[rag-tender](https://github.com/HunterLzap/rag-tender) ·
[BiaoShu-SKILL](https://github.com/Get00/BiaoShu-SKILL) ·
[dsh-bidding](https://github.com/longsky21/dsh-bidding)

解析底座：[MinerU](https://github.com/opendatalab/MinerU) · [PaddleOCR](https://github.com/PaddlePaddle/PaddleOCR)

## 📄 License

[MIT](LICENSE)
