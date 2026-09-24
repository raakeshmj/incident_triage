# ADR-0017: Alertmanager reaches the host-run API via `host.docker.internal`

Status: Accepted

## Context

Phase 3 adds a real, containerized Alertmanager
(`infrastructure/alertmanager/alertmanager.yml`) that must deliver
webhook notifications to `POST /api/v1/alerts/alertmanager`. Phase 1/2's
API, however, runs on the host via `make run-api` (uvicorn with
`--reload`), not inside a container -- that's the existing hot-reload dev
workflow, and nothing about Phase 3 requires changing it.

## Decision

Alertmanager's webhook URL is
`http://host.docker.internal:8000/api/v1/alerts/alertmanager`, and the
`alertmanager` service in `docker-compose.yml` gets
`extra_hosts: ["host.docker.internal:host-gateway"]` so that hostname
resolves to the Docker host from inside the container (Docker Engine
20.10+, Linux and Desktop alike).

## Alternatives considered

- **Containerize the API for Phase 3.** Would let Alertmanager reach it
  by service name like everything else in `docker-compose.yml`, but
  changes Phase 1/2's established local-dev workflow (hot-reload via
  `make run-api`) for a phase that isn't supposed to touch it, and adds
  its own complications (mounting source for reload, matching the venv's
  dependency set inside a new API image). Rejected: out of scope for
  "build an environment the platform can observe" and not something the
  brief asked to change.
- **Run Alertmanager on the host too** (not containerized). Rejected:
  the brief explicitly asks for Alertmanager as part of the
  docker-composed observability stack, and it needs to co-locate with
  Prometheus (`alerting.alertmanagers` target) which is containerized.

## Consequences

- `host.docker.internal:host-gateway` is a local-dev-only routing detail;
  it has no equivalent meaning in a real deployment (there, Alertmanager
  and incident-core would both be services in the same cluster,
  addressed normally). This is fine -- `docs/architecture/12-local-development.md`
  already establishes that local topology mirrors trust boundaries, not
  exact production networking.
- If a developer runs the API on a different port (`API_PORT` in `.env`),
  `infrastructure/alertmanager/alertmanager.yml`'s hardcoded `:8000` needs
  updating too -- there's no indirection here. Acceptable for a local-dev
  config file editable in one place.
