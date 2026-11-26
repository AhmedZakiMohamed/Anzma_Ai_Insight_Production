"""Tool registry: registration, lookup, listing for the model, and one safe call path."""
from __future__ import annotations

from typing import Any, Mapping

from app.domain.tools.envelope import Envelope, Limitation, LimitationCode, ResultStatus, cannot_compute
from app.domain.tools.spec import ToolSpec


class ToolRegistryError(Exception):
    pass


class ToolRegistry:
    def __init__(self, specs: tuple[ToolSpec, ...] | list[ToolSpec] = ()):
        self._by_name: dict[str, ToolSpec] = {}
        for spec in specs:
            self.register(spec)

    def register(self, spec: ToolSpec) -> ToolSpec:
        if spec.name in self._by_name:
            raise ToolRegistryError(f"tool already registered: {spec.name}")
        if spec.model_name in {s.model_name for s in self._by_name.values()}:
            raise ToolRegistryError(f"tool name collides after model-name mapping: {spec.name}")
        self._by_name[spec.name] = spec
        return spec

    def get(self, name: str) -> ToolSpec | None:
        """Accepts the canonical dotted name or the model-facing name."""
        if name in self._by_name:
            return self._by_name[name]
        return next((s for s in self._by_name.values() if s.model_name == name), None)

    def __contains__(self, name: str) -> bool:
        return self.get(name) is not None

    def __len__(self) -> int:
        return len(self._by_name)

    def names(self) -> list[str]:
        return sorted(self._by_name)

    def agent_tools(self) -> list[dict[str, Any]]:
        """Entries ready to be appended to ``AGENT_TOOLS`` (sorted, so prompts and tests are stable)."""
        return [self._by_name[n].to_agent_tool() for n in self.names()]

    def call(self, name: str, arguments: Mapping[str, Any] | None = None) -> Envelope:
        """Validate then run a tool. Always returns an Envelope; never raises for bad model input.

        Unknown tools raise ``ToolRegistryError`` (a caller bug, not model input); bad arguments come back as a
        CANNOT_COMPUTE envelope so the model sees a structured reason instead of a stack trace.
        """
        spec = self.get(name)
        if spec is None:
            raise ToolRegistryError(f"unknown tool: {name}")
        args = dict(arguments or {})
        problems = spec.validate_arguments(args)
        if problems:
            return cannot_compute(tool=spec.name, query_version=spec.version, parameters=args,
                                  limitation=Limitation(LimitationCode.INVALID_PARAMETER.value, "; ".join(problems),
                                                        {"problems": problems}))
        result = spec.handler(**args)
        if not isinstance(result, Envelope):
            raise ToolRegistryError(f"{spec.name} handler must return an Envelope, got {type(result).__name__}")
        if result.tool != spec.name or result.query_version != spec.version:
            raise ToolRegistryError(f"{spec.name} handler returned an envelope for {result.tool}@{result.query_version}")
        return result


DEFAULT_REGISTRY = ToolRegistry()  # real tools (doors.after_hours, devices.offline_frequency) register here later
