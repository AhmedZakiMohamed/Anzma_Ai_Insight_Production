"""Provider-neutral contract used by AI application/domain services."""
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ModelReply:
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    prompt_tokens: int | None = None


class ModelProvider(Protocol):
    """Small chat-completion port; adapters translate this contract to vendor APIs."""

    def complete(self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None,
                 response_format: str | dict[str, Any] | None = None,
                 options: dict[str, Any] | None = None, keep_alive: str | None = None) -> ModelReply:
        ...
