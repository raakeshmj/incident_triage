# ADR-0020: Investigation loop checkpoints through incident-core, and decision tools

Status: Accepted

## Context

`07-agent-tool-architecture.md` describes the investigation agent as
stateless: it runs the whole tool-use loop and returns **one structured
result** (`submit_findings`) that incident-core validates and persists. Phase
5's requirements contradict that shape in three concrete ways:

1. **Resumability.** A crash mid-investigation must not lose the
   investigation or re-bill model calls already made. With one result at the
   end, everything before it lives only in worker memory.
2. **Persistent hypotheses with a lifecycle.** Hypotheses must be proposed,
   supported, weakened and rejected *during* the loop, each change validated
   against accessible evidence and auditable — not reconstructed from a
   final ranked list.
3. **Deterministic stopping.** The application, not the model, decides when
   a conclusion is acceptable. That requires the application to see the
   conclusion attempt, reject it with reasons, and let the loop continue.

The doc's status vocabularies also differ from Phase 5's required ones.

## Decision

1. **Checkpoint commands, one writer.** The engine (in the worker) sends
   fenced commands to `InvestigationCoreService` after every step:
   `claim`, `record_step`, `apply_hypothesis_updates`, `complete`,
   `escalate`. incident-core remains the only writer of `investigations`,
   `hypotheses`, `hypothesis_evidence_links`, `investigation_steps`,
   `rca_reports` and the incident's status. The engine still has no database
   access of its own beyond calling that service (a boundary test enforces
   that `packages/agents` imports no DB/Redis/HTTP/subprocess).
2. **Leases fence writers.** `claim` sets `lease_owner`/`lease_expires_at`;
   every write verifies the owner and raises `LeaseLostError` otherwise. An
   expired lease makes the investigation resumable by any worker; the
   transcript is rebuilt from `investigation_steps`.
3. **Decision tools replace `submit_findings`.** The model ends or advances
   the investigation through three tools: `update_hypotheses`,
   `conclude_investigation`, `declare_inconclusive`. A conclusion is
   *proposed* by the model and *accepted or rejected* by incident-core
   against `StoppingCriteria`; a rejection is fed back. The Phase 4 draft
   schema `packages/tools/findings.py` is removed. Remediation proposals are
   not part of Phase 5 at all.
4. **Status vocabularies.** Investigation: `CREATED → INVESTIGATING →
   COMPLETED | ESCALATED | FAILED` (doc's `running|completed|inconclusive|failed`:
   `running` = `INVESTIGATING`, `inconclusive` = `ESCALATED`, `CREATED`
   added for the queued-but-unclaimed state). Hypothesis: `ACTIVE`,
   `SUPPORTED`, `WEAKENED`, `REJECTED`, `SELECTED` (doc's `proposed`,
   `supported`, `refuted`, `selected_root_cause`; `WEAKENED` added so a
   hypothesis can lose ground without being final). `SELECTED` is set only
   by incident-core on an accepted conclusion.
5. **Accessible evidence** for an investigation = evidence ids it was shown
   (context step, tool-call steps) ∩ `evidence_refs` for its incident,
   computed by incident-core. Hypothesis links carry an FK to
   `evidence_refs`. Steps, links and RCA reports are immutable (trigger).
6. **Retries of a whole investigation** still create a new `Investigation`
   row (`attempt_number`); resuming the *same* investigation after a crash is
   not a retry.

## Alternatives considered

- **Keep one final result, add worker-local checkpoints (files/Redis).**
  Rejected: splits state across stores, and Redis is explicitly not a
  source of truth (ADR-0003).
- **Let the worker write investigation tables directly.** Rejected: breaks
  the single-writer rule (ADR-0001/0002) and the schema-per-role boundary.
- **SDK tool runner loop.** Rejected: the application must persist,
  validate and budget each step before the next model call; a manual loop
  keeps that explicit.

## Consequences

- Every model turn and tool call is a durable row: full audit and exact
  replay, at the cost of more writes per investigation (tens, not
  thousands).
- The liveness guarantee is the resume sweep, not event delivery.
- The engine depends on a `InvestigationGateway` Protocol; an HTTP
  implementation (worker in a separate deployment) is a transport change,
  not a design change.
