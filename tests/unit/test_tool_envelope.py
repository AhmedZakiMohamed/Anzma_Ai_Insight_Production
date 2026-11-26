"""Envelope / ToolSpec / registry contract tests. No database, FastAPI or live model required."""
import json
import unittest
from datetime import datetime, timedelta, timezone

from app.adapters.model_providers.ollama_provider import OllamaProvider
from app.domain.tools.envelope import (ENVELOPE_FIELDS, EnvelopeBuilder, EvidenceRef, Limitation, LimitationCode,
                                       ResultStatus, Row, cannot_compute, compute_result_id, iso_utc, unsupported,
                                       worst_quality)
from app.domain.tools.registry import ToolRegistry, ToolRegistryError
from app.domain.tools.spec import ToolSpec

T0 = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)
CLOCK = lambda: T0 + timedelta(minutes=5)


def builder(**kw):
    base = dict(tool="demo.echo", query_version="1", parameters={"n": 2}, data_as_of=T0, source_ids=["b", "a", "a"],
                columns=["asset", "count"], clock=CLOCK, id_factory=lambda: "trace-1")
    base.update(kw)
    return EnvelopeBuilder(**base)


def rows(n):
    return [Row({"asset": f"d{i}", "count": i}, (EvidenceRef("event", str(i), T0),)) for i in range(n)]


class EnvelopeTests(unittest.TestCase):
    def test_complete_zero_rows_is_legitimate(self):  # G-COMP-03
        env = builder(rows=[]).build()
        d = env.to_dict()
        self.assertTrue(d["complete"])
        self.assertEqual((d["total_rows"], d["rows"], d["status"]), (0, [], "COMPLETE"))

    def test_all_adr_fields_present_in_order(self):
        d = builder(rows=rows(1)).build().to_dict()
        self.assertEqual(list(d)[:len(ENVELOPE_FIELDS)], list(ENVELOPE_FIELDS))
        self.assertEqual(d["source_ids"], ["a", "b"])
        self.assertEqual(d["trace_id"], "trace-1")
        json.dumps(d)  # fully JSON-serialisable

    def test_rows_carry_evidence_refs(self):
        d = builder(rows=rows(2)).build().to_dict()
        self.assertEqual(d["rows"][1]["evidence_refs"], [{"kind": "event", "id": "1", "observed_at": "2026-11-24T12:00:00Z"}])
        self.assertEqual(d["rows"][1]["count"], 1)

    def test_row_outside_declared_columns_rejected(self):
        with self.assertRaises(ValueError):
            builder(rows=[Row({"asset": "x", "surprise": 1})]).build()

    def test_truncation_marks_incomplete_and_keeps_total(self):
        env = builder(rows=rows(5), max_rows=2).build()
        d = env.to_dict()
        self.assertTrue(d["truncated"])
        self.assertFalse(d["complete"])
        self.assertEqual((len(d["rows"]), d["total_rows"]), (2, 5))
        self.assertEqual(d["limitations"][0]["code"], "ROWS_TRUNCATED")

    def test_partial_requires_limitation_and_allows_partial_rows(self):  # G-COMP-01
        with self.assertRaises(ValueError):
            builder(rows=rows(1), status=ResultStatus.PARTIAL).build()
        lim = Limitation(LimitationCode.PAGE_FAILED, "Page 2 of 3 failed.")
        d = builder(rows=rows(1), status=ResultStatus.PARTIAL, limitations=[lim]).build().to_dict()
        self.assertFalse(d["complete"])
        self.assertEqual((d["status"], d["total_rows"]), ("PARTIAL", 1))

    def test_unsupported_is_not_zero(self):  # G-COMP-02
        d = unsupported(tool="demo.echo", query_version="1", parameters={}, columns=["asset"],
                        limitation=Limitation(LimitationCode.CAPABILITY_UNSUPPORTED, "history.events not declared")).to_dict()
        self.assertEqual((d["status"], d["complete"], d["rows"]), ("UNSUPPORTED", False, []))

    def test_cannot_compute_has_reason_and_no_rows(self):  # G-TZ-01
        env = cannot_compute(tool="demo.echo", query_version="1", parameters={},
                             limitation=Limitation(LimitationCode.TIMEZONE_UNAVAILABLE, "No reliable timezone."))
        self.assertEqual((env.status, env.total_rows, env.complete), (ResultStatus.CANNOT_COMPUTE, 0, False))
        with self.assertRaises(ValueError):
            builder(rows=rows(1), status=ResultStatus.CANNOT_COMPUTE,
                    limitations=[Limitation(LimitationCode.TIMEZONE_UNAVAILABLE, "x")]).build()

    def test_missing_data_as_of_is_disclosed_not_invented(self):
        d = builder(data_as_of=None).build().to_dict()
        self.assertIsNone(d["data_as_of"])
        self.assertIn("DATA_AS_OF_UNKNOWN", [x["code"] for x in d["limitations"]])

    def test_naive_datetime_rejected(self):  # D-03
        with self.assertRaises(ValueError):
            iso_utc(datetime(2026, 11, 1, 20, 0))

    def test_unknown_limitation_code_rejected(self):
        with self.assertRaises(ValueError):
            Limitation("MADE_UP", "nope")

    def test_quality_validation_and_worst(self):
        with self.assertRaises(ValueError):
            builder(quality="MISSING").build()  # contract D1: MISSING is not a quality value
        self.assertEqual(worst_quality(["GOOD", "BAD", None, "UNCERTAIN"]), "BAD")
        self.assertIsNone(worst_quality([None]))


class ResultIdTests(unittest.TestCase):
    def test_deterministic_and_order_insensitive(self):
        a = compute_result_id("t", {"a": 1, "b": 2}, T0, "1")
        self.assertEqual(a, compute_result_id("t", {"b": 2, "a": 1}, T0, "1"))
        self.assertTrue(a.startswith("res_") and len(a) == 36)

    def test_changes_with_every_input_but_not_generated_at(self):
        base = compute_result_id("t", {"a": 1}, T0, "1")
        for other in (compute_result_id("u", {"a": 1}, T0, "1"), compute_result_id("t", {"a": 2}, T0, "1"),
                      compute_result_id("t", {"a": 1}, T0 + timedelta(seconds=1), "1"),
                      compute_result_id("t", {"a": 1}, T0, "2")):
            self.assertNotEqual(base, other)
        e1 = builder().build()
        e2 = builder(clock=lambda: T0 + timedelta(days=1)).build()
        self.assertEqual(e1.result_id, e2.result_id)

    def test_trace_id_is_per_call_by_default(self):
        b = EnvelopeBuilder(tool="t", query_version="1", data_as_of=T0)
        self.assertNotEqual(b.build().trace_id, b.build().trace_id)


def echo_handler(n):
    return builder(parameters={"n": n}, rows=rows(n)).build()


def make_spec(**kw):
    base = dict(name="demo.echo", description="Echo n rows.", version="1", handler=echo_handler,
                parameters={"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]})
    base.update(kw)
    return ToolSpec(**base)


class SpecAndRegistryTests(unittest.TestCase):
    def test_agent_tool_shape_matches_agent_tools_entries(self):
        tool = make_spec().to_agent_tool()
        self.assertEqual(set(tool), {"name", "description", "parameters"})  # same keys as run_sql in chat_lite.AGENT_TOOLS
        self.assertEqual(tool["name"], "demo__echo")
        self.assertNotIn("handler", tool)

    def test_ollama_adapter_accepts_the_converted_tool_unchanged(self):
        wrapped = OllamaProvider._tool_for_ollama(make_spec().to_agent_tool())
        self.assertEqual(wrapped["type"], "function")
        self.assertEqual(wrapped["function"]["name"], "demo__echo")

    def test_invalid_specs_rejected(self):
        for bad in (dict(name="Demo"), dict(name="demo."), dict(description=" "), dict(version=""),
                    dict(parameters={"type": "string"}),
                    dict(parameters={"type": "object", "properties": {}, "required": ["n"]})):
            with self.assertRaises((ValueError, TypeError)):
                make_spec(**bad)

    def test_register_lookup_and_duplicates(self):
        reg = ToolRegistry([make_spec()])
        self.assertIn("demo.echo", reg)
        self.assertIs(reg.get("demo__echo"), reg.get("demo.echo"))
        self.assertEqual(reg.names(), ["demo.echo"])
        with self.assertRaises(ToolRegistryError):
            reg.register(make_spec())
        # "demo.echo" and "demo__echo" would be the same name to the model, so the second is refused too
        with self.assertRaises(ToolRegistryError):
            reg.register(make_spec(name="demo.echo"))
        reg.register(make_spec(name="demo.other"))
        self.assertEqual(reg.names(), ["demo.echo", "demo.other"])

    def test_call_by_model_name_returns_envelope(self):
        d = ToolRegistry([make_spec()]).call("demo__echo", {"n": 2}).to_dict()
        self.assertEqual((d["tool"], d["total_rows"], d["query_version"]), ("demo.echo", 2, "1"))

    def test_bad_arguments_become_cannot_compute_not_exceptions(self):
        reg = ToolRegistry([make_spec()])
        for args in ({}, {"n": "2"}, {"n": True}, {"n": 1, "extra": 1}):
            env = reg.call("demo.echo", args)
            self.assertEqual(env.status, ResultStatus.CANNOT_COMPUTE, args)
            self.assertEqual(env.limitations[0].code, "INVALID_PARAMETER")

    def test_unknown_tool_is_a_caller_error(self):
        with self.assertRaises(ToolRegistryError):
            ToolRegistry().call("nope", {})

    def test_handler_must_return_matching_envelope(self):
        reg = ToolRegistry([make_spec(handler=lambda n: {"rows": []})])
        with self.assertRaises(ToolRegistryError):
            reg.call("demo.echo", {"n": 1})
        reg = ToolRegistry([make_spec(version="2")])  # handler stamps version "1"
        with self.assertRaises(ToolRegistryError):
            reg.call("demo.echo", {"n": 1})


if __name__ == "__main__":
    unittest.main()
