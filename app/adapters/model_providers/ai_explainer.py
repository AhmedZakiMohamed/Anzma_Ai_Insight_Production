"""Compatibility facade for the former provider-specific alert explainer.

New application/domain code uses ``app.domain.ai.explanation`` and receives a
provider through the ``ModelProvider`` contract. Ollama's HTTP implementation
lives in ``ollama_provider.py``.
"""
import os

from app.adapters.model_providers.ollama_provider import OllamaProvider
from app.domain.ai.explanation import explain as _explain
from app.domain.ai.recommendations import eligible
from app.domain.evidence.grounding import (
    build_package, check_text, normalize_summary, parse_citations,
)

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:3b")
OLLAMA_TIMEOUT = int(os.getenv("OLLAMA_TIMEOUT", "120"))


def _provider():
    return OllamaProvider(host=OLLAMA_HOST.rstrip("/"), model=OLLAMA_MODEL, timeout=OLLAMA_TIMEOUT)


def call_ollama(messages):
    """Legacy helper. Prefer injecting ``OllamaProvider`` at the application boundary."""
    return _provider().complete(messages=messages, response_format="json", options={"temperature": 0}).content


def explain(alert, asset, use_llm=True):
    """Legacy helper retained for scripts; production paths inject the provider explicitly."""
    return _explain(alert, asset, use_llm=use_llm, provider=_provider() if use_llm else None)
