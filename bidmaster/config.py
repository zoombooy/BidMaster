"""全局配置：环境变量 + .env，全部可选——不配 LLM 也能跑纯规则模式。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

try:  # 可选：项目根放 .env 即可生效
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:  # pragma: no cover
    pass


@dataclass
class Settings:
    # LLM 兜底通道（OpenAI 兼容端点：DeepSeek / GLM / Qwen / Ollama 均可）
    llm_base_url: str = field(default_factory=lambda: os.getenv("BIDMASTER_LLM_BASE_URL", ""))
    llm_api_key: str = field(default_factory=lambda: os.getenv("BIDMASTER_LLM_API_KEY", ""))
    llm_model: str = field(default_factory=lambda: os.getenv("BIDMASTER_LLM_MODEL", ""))
    llm_timeout: float = field(default_factory=lambda: float(os.getenv("BIDMASTER_LLM_TIMEOUT", "60")))

    # OCR 兜底通道（扫描件）：mineru | paddle | 空=不启用（扫描页记 FAILED_REVIEW）
    ocr_backend: str = field(default_factory=lambda: os.getenv("BIDMASTER_OCR_BACKEND", ""))

    # PDF 文本层判定阈值：平均每页有效字符数低于该值视为扫描件
    pdf_text_min_chars_per_page: int = field(
        default_factory=lambda: int(os.getenv("BIDMASTER_PDF_MIN_CHARS", "30")))

    # 置信度低于阈值 → 人工复核队列
    review_threshold: float = field(default_factory=lambda: float(os.getenv("BIDMASTER_REVIEW_THRESHOLD", "0.7")))

    @property
    def llm_enabled(self) -> bool:
        return bool(self.llm_base_url and self.llm_model)


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
