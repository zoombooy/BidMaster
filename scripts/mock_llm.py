"""本地 Mock LLM 端点（OpenAI /chat/completions 兼容）——用于离线演示/测试 AI 兜底链路。

模拟真实大模型的行为：读 prompt → 返回结构化 JSON（含逐字取自原文的 quote）。
真实使用时无需本脚本：配置 BIDMASTER_LLM_BASE_URL 指向 GLM/DeepSeek/Qwen 等任意
OpenAI 兼容端点即可，管线代码完全相同。

用法：python scripts/mock_llm.py [端口]   # 默认 8901
"""
from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

# 预制的"模型回答"（quote 逐字取自前附表之五原文，代码回查必须能命中）
MOCK_ANSWER = {
    "formulas": [
        {"name": "公式1：最低评标价法（金额）",
         "expression": "价格得分=（基准价/投标人的评标价）×100",
         "baseline": "基准价=所评包或标段中所有通过商务、技术评审的合格投标人评标价中的最低价"},
        {"name": "公式2：最低评标价法（折扣比例）",
         "expression": "价格得分=（基准值）/（投标人的折扣比例）×100",
         "baseline": "基准值=所评包或标段中所有通过商务、技术评审的合格投标人折扣比例中的最低值"},
        {"name": "公式3：算术平均值下浮法（金额）",
         "expression": "价格得分=100-100×n×|基准价-投标人的评标价|/基准价（评标价≤基准价时n=1，＞基准价时n=2）",
         "baseline": "基准价为合格投标人评标价去掉部分高价和低价后的算术平均值乘以（1-下浮系数）"},
    ],
    "quote": "公式1：最低评标价法（金额） 价格得分=（基准价/投标人的评标价）×100",
}


class MockLLMHandler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.path and self.rfile.read(length) or b"{}")
        prompt = ""
        for m in body.get("messages", []):
            prompt += m.get("content", "")
        print(f"[mock-llm] 收到请求 {len(prompt)} 字符，返回结构化公式 JSON")
        content = json.dumps(MOCK_ANSWER, ensure_ascii=False)
        resp = {"jsonrpc": "2.0", "id": body.get("id"),
                "choices": [{"message": {"role": "assistant", "content": content},
                             "finish_reason": "stop"}]}
        data = json.dumps(resp, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):  # 静默
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8901
    print(f"Mock LLM 端点: http://127.0.0.1:{port}/chat/completions")
    HTTPServer(("127.0.0.1", port), MockLLMHandler).serve_forever()
