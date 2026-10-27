"""Environment-backed model configuration.

The project root `.env` is loaded first; real shell variables win. The file is
ignored by git, `.env.example` is the tracked template.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
logger = logging.getLogger(__name__)


def build_model():
    from .model import MockModel, OpenAIModel

    provider = os.getenv("LLM_PROVIDER", "mock").strip().lower()
    if provider in ("", "mock", "none"):
        return MockModel()
    if provider in ("openai", "deepseek"):
        api_key = os.getenv("LLM_API_KEY", "").strip()
        if not api_key:
            logger.warning(
                "LLM_PROVIDER=openai but LLM_API_KEY is empty; using MockModel"
            )
            return MockModel()
        return OpenAIModel(
            base_url=os.getenv("LLM_BASE_URL", "https://api.deepseek.com"),
            api_key=api_key,
            model=os.getenv("LLM_MODEL", "deepseek-chat"),
            temperature=float(os.getenv("LLM_TEMPERATURE", "0.2")),
            timeout=float(os.getenv("LLM_TIMEOUT", "120")),
        )
    raise RuntimeError(f"unsupported LLM_PROVIDER: {provider!r}")
