"""Ollama implementation of the provider-neutral chat completion contract."""
import json
import os
import urllib.request
from dataclasses import dataclass
from typing import Any

from app.domain.ai.model_provider import ModelReply, ModelProvider, ToolCall


@dataclass(frozen=True)
class OllamaProvider:
    host: str = "http://localhost:11434"
    model: str = "qwen2.5:3b"
    timeout: int = 120

    @classmethod
    def from_env(cls) -> "OllamaProvider":
        return cls(
            host=os.getenv("OLLAMA_HOST", "http://localhost:11434").rstrip("/"),
            model=os.getenv("OLLAMA_MODEL", "qwen2.5:3b"),
            timeout=int(os.getenv("OLLAMA_TIMEOUT", "120")),
        )

    def complete(self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None,
                 response_format: str | dict[str, Any] | None = None,
                 options: dict[str, Any] | None = None, keep_alive: str | None = None) -> ModelReply:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [self._message_for_ollama(message) for message in messages],
            "stream": False,
            "options": options or {},
        }
        if tools:
            body["tools"] = [self._tool_for_ollama(tool) for tool in tools]
        if response_format is not None:
            body["format"] = response_format
        if keep_alive is not None:
            body["keep_alive"] = keep_alive

        request = urllib.request.Request(
            f"{self.host}/api/chat", data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            payload = json.loads(response.read())

        message = payload.get("message") or {}
        calls = []
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            arguments = function.get("arguments") or {}
            if isinstance(arguments, str):
                arguments = json.loads(arguments or "{}")
            calls.append(ToolCall(name=function.get("name", ""), arguments=arguments))
        return ModelReply(
            content=message.get("content") or "",
            tool_calls=tuple(calls),
            prompt_tokens=payload.get("prompt_eval_count"),
        )

    @staticmethod
    def _message_for_ollama(message: dict[str, Any]) -> dict[str, Any]:
        result = {key: value for key, value in message.items() if key not in ("tool_calls", "name")}
        if message.get("role") == "tool" and message.get("name"):
            result["tool_name"] = message["name"]
        if message.get("tool_calls"):
            result["tool_calls"] = [
                {"function": {"name": call["name"], "arguments": call.get("arguments") or {}}}
                for call in message["tool_calls"]
            ]
        return result

    @staticmethod
    def _tool_for_ollama(tool: dict[str, Any]) -> dict[str, Any]:
        if "function" in tool:  # Accept legacy Ollama-shaped declarations during migration.
            return tool
        return {"type": "function", "function": tool}
