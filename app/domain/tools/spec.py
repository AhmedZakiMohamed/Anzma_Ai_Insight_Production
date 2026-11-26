"""ToolSpec: the declaration Amr's chat layer consumes (design note: docs/TOOL_REGISTRY_DESIGN.md).

A ToolSpec is converted to the *existing* ``AGENT_TOOLS`` dict shape (``name``/``description``/``parameters``),
so ``ModelProvider`` and ``OllamaProvider`` are untouched. ``handler`` and ``version`` never reach the model.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from app.domain.tools.envelope import Envelope

ToolHandler = Callable[..., Envelope]


@dataclass(frozen=True)
class ToolSpec:
    name: str                          # dotted, lowercase, e.g. "doors.after_hours"
    description: str
    parameters: Mapping[str, Any]      # JSON Schema, object-typed
    handler: ToolHandler               # called as handler(**arguments) and must return an Envelope
    version: str                       # becomes the envelope's query_version

    def __post_init__(self):
        import re
        if not re.fullmatch(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*", self.name):
            raise ValueError(f"invalid tool name: {self.name!r}")
        if not self.description.strip():
            raise ValueError("tool description is required (the model reads it)")
        if not self.version.strip():
            raise ValueError("tool version is required")
        if not callable(self.handler):
            raise TypeError("handler must be callable")
        schema = self.parameters
        if schema.get("type") != "object" or not isinstance(schema.get("properties"), Mapping):
            raise ValueError("parameters must be a JSON Schema object with 'properties'")
        unknown_required = set(schema.get("required", ())) - set(schema["properties"])
        if unknown_required:
            raise ValueError(f"required names missing from properties: {sorted(unknown_required)}")

    @property
    def model_name(self) -> str:
        """Ollama/OpenAI-style function names reject dots, so the model sees ``doors__after_hours``."""
        return self.name.replace(".", "__")

    def to_agent_tool(self) -> dict[str, Any]:
        """Same dict shape as the entries of ``AGENT_TOOLS`` in chat_lite.py."""
        return {"name": self.model_name, "description": self.description, "parameters": dict(self.parameters)}

    def validate_arguments(self, arguments: Mapping[str, Any] | None) -> list[str]:
        """Minimal structural check (required, unknown keys, primitive types). Returns problems, empty if valid.

        Semantic validation (dates, timezones - ADR D-03/D-10) belongs to the tool itself.
        """
        args = dict(arguments or {})
        props = self.parameters["properties"]
        problems = [f"missing required argument: {n}" for n in self.parameters.get("required", ()) if n not in args]
        problems += [f"unknown argument: {n}" for n in args if n not in props]
        py = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "array": list, "object": dict}
        for key, value in args.items():
            expected = props.get(key, {}).get("type")
            if expected in py and not (isinstance(value, py[expected]) and
                                       not (expected in ("integer", "number") and isinstance(value, bool))):
                problems.append(f"argument {key} must be of type {expected}")
        return problems
