# packages/tools

The investigation tool contracts from
`docs/architecture/07-agent-tool-architecture.md` -- the only interface a
future investigation agent will have. **Not connected to Claude in Phase 4.**

```
agent tool call -> ToolExecutor -> EvidenceService -> adapter -> telemetry backend
```

- `contracts.py` -- strict (`extra="forbid"`) input schemas per tool, with
  closed enumerations (metric names, severities, trace search modes);
  `ToolContext` (incident/investigation bound by the orchestrator, never the
  model); `ToolSuccess` (compact: `evidence_id`, `content_hash`, summary,
  normalized data) and `ToolFailure` (stable error code, no backend text).
- `registry.py` -- `TOOLS`: `get_metrics`, `get_service_health`, `get_logs`,
  `get_trace`, `get_traces`, `get_deploys`, `get_config_history`,
  `get_git_diff`, `get_recent_commits`, `search_historical_incidents`; each
  mapped onto one evidence-service operation. `tool_definitions()` emits
  name/description/JSON Schema for all of them plus `submit_findings`.
- `executor.py` -- validation, retry of transient backend errors, per
  investigation budgets (total calls, identical-call loop detection).
- `findings.py` -- `submit_findings`' schema (`InvestigationResult`), with the
  exactly-one-of `selected_root_cause_index` / `inconclusive_reason` rule.

No module here imports an HTTP/Redis/DB client, an adapter, or incident-core
(`tests/unit/test_boundaries.py`): the tool layer's only path to telemetry is
the evidence service.
