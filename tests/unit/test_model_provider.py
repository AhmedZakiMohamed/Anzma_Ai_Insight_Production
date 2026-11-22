"""Provider contract tests; no database or live model is required."""
import json
import unittest
from unittest.mock import patch

from app.adapters.model_providers.ollama_provider import OllamaProvider
from app.domain.ai.model_provider import ModelReply, ToolCall
from app.domain.evidence.validation import explain_alert


ALERT = {
    "id": 41,
    "rule_id": "HVAC_HIGH_TEMP",
    "severity": "WARNING",
    "summary": "High temperature recorded for CRAC Unit 1.",
    "detection_method": "threshold",
    "asset_id": 1,
    "first_ts": "2026-11-16T12:00:00+00:00",
    "last_ts": "2026-11-16T12:00:00+00:00",
    "evidence": {
        "thresholds": {"warning": 29.0},
        "facts": {"max_value": 33.1, "fan_off": True, "sustained": True, "zone": "Server Room"},
        "event_count": 1,
        "events": [{"ref": "E1", "ts": "2026-11-16T12:00:00+00:00",
                    "event_type": "TEMPERATURE_READING", "value": 33.1,
                    "unit": "°C", "details": {}}],
    },
}
ASSET = {"id": 1, "name": "CRAC Unit 1", "system_type": "HVAC", "zone": "Server Room"}


class FakeProvider:
    model = "fake-model"

    def complete(self, **_kwargs):
        return ModelReply(content=json.dumps({
            "summary": "The temperature reached 33.1 °C [E1].",
            "cause_ids": [], "action_ids": [], "evidence_refs": ["E1"], "confidence": "low",
        }))


class FailingProvider:
    def complete(self, **_kwargs):
        raise TimeoutError("simulated model outage")


class Response:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps({
            "message": {"content": "", "tool_calls": [{"function": {
                "name": "run_sql", "arguments": {"sql": "SELECT 1"},
            }}]},
            "prompt_eval_count": 12,
        }).encode()


class ModelProviderTests(unittest.TestCase):
    def test_grounded_model_response_passes_through_injected_provider(self):
        result = explain_alert(ALERT, ASSET, provider=FakeProvider())
        self.assertEqual(result["ai_source"], "llm")
        self.assertEqual(result["model_used"], "fake-model")
        self.assertEqual(result["validation"]["status"], "grounding_supported")

    def test_provider_failure_uses_grounded_deterministic_fallback(self):
        result = explain_alert(ALERT, ASSET, provider=FailingProvider())
        self.assertEqual(result["ai_source"], "fallback")
        self.assertEqual(result["validation"]["status"], "grounding_supported")
        self.assertTrue(any("language model was unavailable" in item for item in result["limitations"]))

    def test_ollama_adapter_translates_provider_tool_contract(self):
        captured = {}

        def fake_urlopen(request, timeout):
            captured["body"] = json.loads(request.data)
            captured["timeout"] = timeout
            return Response()

        provider = OllamaProvider(host="http://ollama:11434", model="qwen2.5:3b", timeout=17)
        with patch("app.adapters.model_providers.ollama_provider.urllib.request.urlopen", fake_urlopen):
            reply = provider.complete(
                messages=[{"role": "user", "content": "query"}],
                tools=[{"name": "run_sql", "description": "read", "parameters": {"type": "object"}}],
            )

        self.assertEqual(captured["timeout"], 17)
        self.assertEqual(captured["body"]["model"], "qwen2.5:3b")
        self.assertEqual(captured["body"]["tools"][0]["function"]["name"], "run_sql")
        self.assertEqual(reply.tool_calls, (ToolCall("run_sql", {"sql": "SELECT 1"}),))
        self.assertEqual(reply.prompt_tokens, 12)


if __name__ == "__main__":
    unittest.main()
