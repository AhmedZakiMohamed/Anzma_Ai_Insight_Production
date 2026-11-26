# Tool envelope, ToolSpec and registry (design note)

- **Status:** Implemented as additive code; **needs Amr's review** before chat consumes it (`docs/CHANGE_COORDINATION.md`: chat and the model interface are Amr's).
- **Scope:** envelope + `ToolSpec` + registry only. `doors.after_hours` and `devices.offline_frequency` are **not** implemented here.
- **Code:** `app/domain/tools/{envelope,spec,registry}.py`; tests: `tests/unit/test_tool_envelope.py`. No DB, FastAPI or model imports.

## What Amr consumes

```python
from app.domain.tools.registry import DEFAULT_REGISTRY

AGENT_TOOLS += DEFAULT_REGISTRY.agent_tools()      # same dict shape as run_sql: name / description / parameters

# in the tool loop, next to run_sql / explain_alert:
if name in DEFAULT_REGISTRY:
    envelope = DEFAULT_REGISTRY.call(name, args)    # always returns an Envelope for bad arguments
    result = json.dumps(envelope.to_dict(), ensure_ascii=False)
```

`ModelProvider`, `ToolCall` and `OllamaProvider` are **unchanged**. `ToolSpec.to_agent_tool()` produces exactly the dict `OllamaProvider._tool_for_ollama` already wraps (a test proves it).

**Name mapping.** Canonical names are dotted (`doors.after_hours`). Function names sent to the model use `__` (`doors__after_hours`) because dots are not valid in OpenAI/Ollama-style function names. `registry.get/call` accept both. `run_sql` and `explain_alert` are untouched and stay in `AGENT_TOOLS`.

**Row cap.** `chat_lite` currently truncates tool output with `result[:2500]`; cutting an envelope's JSON mid-string would corrupt it. Amr should cap with `EnvelopeBuilder(max_rows=...)` (sets `truncated`, keeps `total_rows`) rather than slicing the string. Not changed here.

## Envelope

All ADR §1 fields, in fixed order, plus `status`:

`result_id, tool, parameters, data_as_of, generated_at, source_ids, complete, quality, truncated, columns, rows[…, evidence_refs], total_rows, limitations, query_version, trace_id, status`

| Decision | Choice | Notes |
|---|---|---|
| `status` | `ResultStatus`: `COMPLETE / PARTIAL / UNSUPPORTED / CANNOT_COMPUTE` | ADR open question g, closed as an enum. Names still need formal ADR acceptance. |
| `complete` | derived: `status == COMPLETE and not truncated` | Tools never set it directly (D-12). |
| `result_id` | `res_` + first 32 hex of SHA-256 over canonical JSON of tool, parameters, `data_as_of`, `query_version` | Deterministic: golden tests and caching. `generated_at` is excluded on purpose. |
| `trace_id` | random UUID4 per call, or passed in by the caller | Identifies a call, not a result, so it is not derived from the inputs. |
| `query_version` | the tool's own `ToolSpec.version` | Bump it whenever the tool's semantics change; it feeds `result_id`. |
| `quality` | `GOOD / UNCERTAIN / BAD` or `null` | Same vocabulary as INGESTION_CONTRACT §7; `MISSING` is rejected. `worst_quality()` aggregates. |
| `limitations` | list of `{code, message, details?}` with `LimitationCode` | Unknown codes raise. Codes map to the ADR decision they come from. |
| `data_as_of` | input from the tool; `null` allowed | Nothing records connector coverage today (ADR g), so a missing value adds `DATA_AS_OF_UNKNOWN` instead of being invented. |
| `source_ids` | input, de-duplicated and sorted | Same reason as above. |

Rules enforced by `EnvelopeBuilder.build()`:

- `UNSUPPORTED` / `CANNOT_COMPUTE` carry **no rows** and `total_rows = 0` (unsupported is never shown as zero, G-COMP-02).
- Any non-`COMPLETE` status must include at least one limitation.
- `COMPLETE` with zero rows is valid (G-COMP-03).
- Row values outside the declared `columns` raise; every row is serialised with exactly the declared columns plus `evidence_refs`.
- Datetimes without an offset are rejected (D-03); output is UTC ISO-8601 with `Z`.

## Not decided here

- Per-tool row schemas (ADR §4 is still Proposed) and the doors/offline logic.
- How `source_ids` / `data_as_of` get populated (ADR open question g, needs the connector completion report).
- Whether `explain_alert` should also return an envelope.
