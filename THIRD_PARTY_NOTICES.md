# Third-Party Notices

本仓库代码为独立实现，未直接拷贝第三方项目源码；但其机制设计参考了以下开源项目，
在此致谢。若未来以本仓库为基础引入上述项目的代码，请同时遵守其原 License。

| 项目 | License | 参考的机制 |
|------|---------|-----------|
| [FB208/OpenBidKit_Yibiao](https://github.com/FB208/OpenBidKit_Yibiao) | AGPL-3.0 | 多任务解析与 prompt 缓存预热、Schema 校验+错误回灌自纠、checkpoint/定向重跑/下游级联失效、"未提及"缺省语义 |
| [guangshu100/BidMaster-Pro](https://github.com/guangshu100/BidMaster-Pro) | AGPL-3.0 | 阶段人工审核闸门（GateKeeper）思想、LiteLLM 多模型降级链思路 |
| [Router0824/BidPilot-AI](https://github.com/Router0824/BidPilot-AI) | Apache-2.0 | 抽取条目的分值/风险/覆盖状态/置信度字段设计 |
| [Inupedia/tender-extract](https://github.com/Inupedia/tender-extract) | MIT | 规则优先+LLM 兜底分层、字段级证据链、F1 评测 CI 门禁 |
| [fanfanyuyang/bid-agent-vscode](https://github.com/fanfanyuyang/bid-agent-vscode) | MIT | 处理账本（无静默遗漏）、字段四态、跨页续表合并 |
| [sanwudazhiyuan/-bid-analysis](https://github.com/sanwudazhiyuan/-bid-analysis) | Apache-2.0 | 三层混合索引（TOC→编号正则→关键词锚区） |
| [Get00/BiaoShu-SKILL](https://github.com/Get00/BiaoShu-SKILL) | Apache-2.0 | 评分标准 JSON 化驱动下游生成的数据流 |
| [railwise-cn/tender-master](https://github.com/railwise-cn/tender-master) | MIT | 评分镜像目录、"不编造"占位符原则 |
| [HunterLzap/rag-tender](https://github.com/HunterLzap/rag-tender) | MIT | 商务要求分类与保守判定策略 |
| [longsky21/dsh-bidding](https://github.com/longsky21/dsh-bidding) | MIT | 确定性脚本做零 token 校验门禁 |

运行时依赖（PyPI）：
python-docx (MIT), PyMuPDF (AGPL-3.0 / 商用许可双授权), pydantic (MIT),
FastAPI (MIT), uvicorn (BSD-3), httpx (BSD-3), python-dotenv (BSD-3)。
可选 OCR 后端：MinerU (代码 Apache-2.0 + 附加条款，模型权重 AGPL-3.0)、
PaddleOCR (Apache-2.0)。个人学习使用均无限制；商用前请复核上述条款。
