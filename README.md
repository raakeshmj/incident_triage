# Incident Intelligence

Autonomous Production Incident Triage & Response platform.

This repository currently contains **architecture and design documentation
only**. No application code has been implemented yet. See [`docs/`](docs/)
for the full design.

- [`docs/README.md`](docs/README.md) — documentation index
- [`docs/architecture/`](docs/architecture/) — component-by-component design
- [`docs/adr/`](docs/adr/) — architecture decision records
- [`docs/review/critical-review.md`](docs/review/critical-review.md) — self-critique
- [`docs/implementation-order.md`](docs/implementation-order.md) — build sequence

## Core rule

**The LLM must never own system state or bypass deterministic controls.**
Claude reasons and proposes. Deterministic code (state machine, policy
engine, action catalog) decides, enforces, and executes. Every claim the
model makes about the world must be backed by a stored, replayable
evidence record — never by the model's own assertion.

## Repository layout (design-stage skeleton)

```
docs/               architecture & decision records (this task's deliverable)
services/           bounded-context service skeletons (README only, no code yet)
apps/web/           Next.js UI skeleton (README only, no code yet)
libs/               shared schema/contract libraries (README only, no code yet)
infra/              docker-compose / kind / k8s manifests (README only, no code yet)
```

See `docs/architecture/02-component-boundaries.md` for what each directory
will eventually own.
