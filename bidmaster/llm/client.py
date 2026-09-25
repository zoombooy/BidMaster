"""LLM 客户端：OpenAI 兼容 /chat/completions（DeepSeek / GLM / Qwen / Ollama 均可）。

未配置时所有调用返回 None，管线自动跳过兜底（纯规则模式照常工作）。
"""
from __future__ import annotations

import json
import re
import time

import httpx

from bidmaster.config import get_settings

_JSON_BLOCK = re.compile(r"\{.*\}|\[.*\]", re.S)


class LLMClient:
    def __init__(self):
        s = get_settings()
        self.base_url = s.llm_base_url.rstrip("/")
        self.api_key = s.llm_api_key
        self.model = s.llm_model
        self.timeout = s.llm_timeout
        self.enabled = s.llm_enabled

    def chat(self, system: str, user: str, *, temperature: float = 0.0,
             json_mode: bool = True, max_tokens: int = 4000) -> str | None:
        if not self.enabled:
            return None
        payload: dict = {
            "model": self.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        url = f"{self.base_url}/chat/completions"
        last_err = None
        for attempt in range(3):  # 重试 3 次（指数退避）
            try:
                r = httpx.post(url, json=payload, headers=headers, timeout=self.timeout)
                r.raise_for_status()
                data = r.json()
                return data["choices"][0]["message"]["content"]
            except Exception as e:  # noqa: BLE001
                last_err = e
                time.sleep(1.5 * (attempt + 1))
        print(f"[llm] 调用失败: {last_err}")
        return None

    def chat_json(self, system: str, user: str, **kw) -> dict | list | None:
        content = self.chat(system, user, json_mode=True, **kw)
        if not content:
            return None
        return parse_json_loose(content)


def parse_json_loose(content: str):
    """从模型输出中宽松提取 JSON（容忍 ```json 包裹与前后杂文）。"""
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        m = _JSON_BLOCK.search(content)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
    return None
