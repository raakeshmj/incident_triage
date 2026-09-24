# 12 — Local Development Architecture

**Implementation status**: Phase 3 (build) implemented this document's
docker-compose layer's observability stack (Prometheus, Loki, Grafana,
plus Alertmanager and an OTel Collector this document didn't originally
list) and three simulated `services/*` processes, at the repo root
`docker-compose.yml` and `simulator/services/` rather than the
`infra/docker-compose/` / `services/*` paths sketched below (Phase 1
already established the actual monorepo layout differently -- see the
root README). See `docs/architecture/14-observability-and-chaos.md`. The
`kind` layer and `evidence-service` record/replay modes remain unstarted.

## Goals

- A developer can run the whole pipeline end-to-end on a laptop.
- No developer needs live production credentials, and ideally no live
  Claude spend, to iterate on anything except the model-facing pieces.
- Local environment topology mirrors production trust boundaries closely
  enough that security assumptions get exercised in dev, not discovered in
  prod.

## Layers

1. **`docker-compose`** (`infra/docker-compose/`): Postgres, Redis,
   Prometheus, Loki, Grafana, and all the `services/*` processes, for fast
   inner-loop development. This is the default for day-to-day work. A
   single Postgres container hosts both the `incident_core` and `evidence`
   logical schemas (see `06-database-design.md` and ADR-0013), each
   provisioned with its own least-privilege role — mirroring the intended
   production topology at this stage, not a dev-only shortcut.
2. **`kind`** (`infra/kind/`): a local Kubernetes cluster for testing
   `remediation-executor`'s Kubernetes adapters and `k8s`-specific
   `action_catalog` entries (restart/rollback/scale) against something
   real, and for rehearsing the actual manifests that will run in
   production (same base kustomize as `infra/k8s/`, kind-specific overlay).
3. **Seed data**: synthetic alert generators (Alertmanager-shaped webhook
   payloads) and a small library of canned incidents, so a fresh
   environment can produce a full TRIAGING→RESOLVED cycle without any real
   monitoring data.

## `evidence-service` record/replay modes (the key dev-cost lever)

- `mode=live`: hits the local docker-compose Prometheus/Loki/kind, for
  testing the real integrations.
- `mode=record`: hits live (local or a designated dev/staging target),
  saves every response as a fixture — used to build eval-harness fixture
  bundles and to snapshot a scenario for a bug repro.
- `mode=replay`: serves saved fixtures — used by `eval-harness`
  (`11-evaluation-architecture.md`) and by any developer who wants a fully
  deterministic, offline scenario (e.g. testing `incident-core`'s state
  machine without standing up Prometheus at all).

This three-mode design means most of the system (`incident-core`,
`policy-engine`, `remediation-executor` against `kind`, `web-ui`) can be
developed and tested with zero live Claude spend and zero live
observability stack, by combining `mode=replay` with a mocked
`investigation-agent` response where even the model call itself isn't
needed.

## Configuration

- Single `.env`-driven config per service (12-factor), no shared global
  config file, so services remain independently deployable.
- `docker-compose.override.yml` (gitignored) for individual developer
  tweaks; the checked-in `docker-compose.yml` is a complete, working
  default.

## Developer workflow (once implemented)

```
make dev-up        # docker-compose up, migrate DB, load action_catalog + policies
make seed-incident # POST a synthetic alert through alert-ingestion
make dev-kind      # stand up kind cluster + deploy a target app for remediation testing
make eval          # run eval-harness against the golden dataset in replay mode
make dev-down
```

`Makefile` targets are the single documented entry point — no tribal
knowledge about the right sequence of commands.

## Local security posture mirrors prod, scaled down

- `remediation-executor`'s kind ServiceAccount is scoped the same way its
  production ServiceAccount will be (namespace-limited, verb-limited) —
  not cluster-admin, even locally — so an over-broad permission is caught
  in dev rather than at a security review right before launch.
- Secrets even in dev come from a local `.env` that's gitignored and
  documented via `.env.example`, not hardcoded into compose files, so the
  habit of "secrets never live in source" holds from day one.
