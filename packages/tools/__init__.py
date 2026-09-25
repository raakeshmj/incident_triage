"""Investigation tool contracts (docs/architecture/07-agent-tool-architecture.md).

The interface a future investigation agent will call -- and the only one it
will have. Every tool is a thin, schema-validated client of the evidence
service; none takes a raw query, a credential, or an incident id (the
incident is bound by `ToolContext`, not chosen by the caller). Phase 4
defines and exercises these contracts; nothing connects them to Claude yet.

    agent tool -> ToolExecutor -> EvidenceService -> adapter -> telemetry backend
"""
