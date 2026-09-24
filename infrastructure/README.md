# infrastructure

Local infrastructure provisioning, referenced by the root
`docker-compose.yml`.

- `postgres/init/` -- SQL run once by Postgres's own
  `docker-entrypoint-initdb.d` mechanism on first container start.
  Creates the `incident_core` and `evidence` logical schemas and their
  respective least-privilege roles (ADR-0013) -- local-dev-only
  credentials, never used in a real deployment (see
  `docs/architecture/13-security-boundaries.md`).

## Phase 3: observability and alerting stack

See `docs/architecture/14-observability-and-chaos.md` for the full design.

- `otel/otel-collector-config.yaml` -- OTLP receiver, `debug` exporter for
  traces from the 3 simulated services. Metrics bypass the collector
  entirely (Prometheus scrapes each service's own `/metrics` directly).
- `prometheus/prometheus.yml` + `prometheus/alerts/service-alerts.yml` --
  scrape config (3 simulated services, `honor_labels: true`) and the 6
  alert rule categories.
- `loki/loki-config.yaml` -- minimal single-binary Loki, filesystem
  storage, local dev only.
- `promtail/promtail-config.yaml` -- scrapes container stdout via the
  Docker socket (Docker service discovery) into Loki; the simulated
  services never talk to Loki directly.
- `grafana/provisioning/` -- datasources (Prometheus + Loki) and a
  dashboard provider, both provisioned automatically on container start;
  `grafana/dashboards/service-overview.json` is the one dashboard
  (request rate, error rate, p95 latency, service health, CPU, memory).
- `alertmanager/alertmanager.yml` -- routes every alert to the existing
  alert-ingestion endpoint's Alertmanager adapter
  (`POST /api/v1/alerts/alertmanager`) via `host.docker.internal`
  (ADR-0017), with a shared-secret Bearer token
  (`ALERTMANAGER_WEBHOOK_TOKEN`).

Kubernetes/kind manifests are reserved for a later phase (Phase 1 has no
Kubernetes dependency at all -- see the Phase 1 brief's explicit
exclusions).
