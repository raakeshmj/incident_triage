# 14 — Observability & Chaos (Phase 3)

Phase 3 builds a realistic local production-like environment for Incident
Intelligence to observe: three simulated services, a full local
observability stack, real Prometheus alerting, and Alertmanager delivering
real alerts into the existing alert-ingestion path. No LLM/investigation
agent and no remediation are implemented here (Phases 5/4) -- this phase
is purely about producing genuine, symptom-only signal for the platform to
correlate.

## Service topology

```
load-generator --5 req/s--> checkout-service --> payment-service --> inventory-service
                                  :8001              :8002               :8003
```

`checkout-service` is the public entry point; `payment-service` charges
the order and then reserves stock via `inventory-service` -- the chain
this phase's spec calls for, so a single distributed trace spans all
three hops and a dependency failure at `inventory-service` genuinely
cascades into `payment-service`'s and (through it) `checkout-service`'s
own error/latency metrics, not a mocked failure.

`load-generator` exists purely to produce the continuous baseline traffic
Prometheus's `rate()`-based alert rules need (a real production checkout
service has continuous customer traffic; a hand-curled `curl` every few
minutes wouldn't ever cross a `rate(...)[1m]` threshold). It is not itself
instrumented -- it's the traffic source, not a system under observation.

## Why the simulator has its own telemetry stack

`simulator/services/common/` (telemetry, chaos, config, http_client) is
deliberately **independent** of `packages/telemetry` -- see
ADR-0016. `packages/telemetry` is Incident Intelligence's own
observability library; the simulated services stand in for *third-party*
production systems Incident Intelligence observes, and a real
checkout/payment/inventory stack would never import Incident
Intelligence's internal packages. Each simulated service ships its own
dependency set (`simulator/services/common/requirements.txt`) and its own
Dockerfile.

## Telemetry architecture

- **Traces**: OpenTelemetry SDK, OTLP/HTTP export to `otel-collector`
  (`infrastructure/otel/otel-collector-config.yaml`), which forwards them
  over OTLP/gRPC to **Grafana Tempo** (Phase 4, `infrastructure/tempo/`:
  single binary, local filesystem storage, 24h block retention, query API
  on :3200). One server span per inbound request
  (`ObservabilityMiddleware`), one client span per outbound call
  (`http_client.call_downstream`), W3C `traceparent` propagated across all
  three hops -- one trace spans checkout -> payment -> inventory.
  *(Phase 3 exported traces to the collector's `debug` exporter only.)*
- **Trace <-> log correlation**: every structured log line carries the
  active span's `trace_id`/`span_id`, so a trace id finds that request's
  log lines in Loki (the evidence Loki adapter's `trace_id` filter, and
  Grafana's Loki `derivedFields` -> Tempo link), and Grafana's Tempo
  datasource links a span back to its service's logs (`tracesToLogsV2`).
  Tempo returns ids with leading zeros stripped; the evidence adapters
  normalize every trace id to 32 lowercase hex characters.
- **Metrics**: an OTel `PrometheusMetricReader`, exposed at each service's
  own `GET /metrics` in Prometheus exposition format. Prometheus scrapes
  each service directly (`infrastructure/prometheus/prometheus.yml`) --
  no collector hop for metrics, which keeps the natural `up{}` metric
  (used by the `ServiceUnavailable` alert) meaningful and avoids an extra
  translation layer. Metrics: `http_requests_total`,
  `http_requests_errors_total`, `http_request_duration_seconds`,
  `http_requests_in_flight`, `dependency_call_duration_seconds`,
  `dependency_call_errors_total`, `process_cpu_usage_ratio`,
  `process_memory_usage_bytes` (both via real `psutil` reads -- see
  "Chaos scenarios" below for why these are never faked), and
  `service_deployment_info` (see "Deployment/version simulation").
- **Logs**: structured JSON to stdout via structlog
  (`timestamp`/`severity`/`service`/`environment`/`region`/`request_id`/
  `trace_id`/`span_id`/`message` + fields), scraped straight off the
  container's log driver by Promtail
  (`infrastructure/promtail/promtail-config.yaml`) into Loki -- no
  application-side Loki client needed.

`prometheus.yml`'s scrape configs use `honor_labels: true`: each service's
own `service` metric attribute wins over the scrape target's `service`
label (same value, avoids Prometheus renaming it `exported_service`),
while `environment`/`region` -- not set on the metrics themselves -- always
come from the scrape target labels.

## Deployment/version simulation

Each service carries `SERVICE_VERSION`/`SERVICE_PREVIOUS_VERSION`/
`SERVICE_DEPLOYED_AT` env vars (docker-compose.yml), exposed as the
`service_deployment_info{service,version,previous_version,environment}`
gauge (always `1`, versions carried as labels -- the standard "info
metric" pattern). The `bad-deployment` chaos scenario overrides these
labels at read-time (`ChaosController.deployment_override()`) to simulate
a rollout of a broken build landing at the same moment as an error-rate
spike -- a real, observable deployment-adjacent signal, distinct from
the alert itself (see "Do not hard-code root cause" below).

## Alert rules (`infrastructure/prometheus/alerts/service-alerts.yml`)

Six categories, each labeled `service`/`environment`/`region` (from the
scrape target or, for `ServiceUnavailable`, from Prometheus's own `up`
series) plus `alert_type`/`severity`, and annotated with
`summary`/`description`/`runbook_url`:

| Alert | Expression basis | `alert_type` |
|---|---|---|
| `HighErrorRate` | 5xx / total request rate > 10% | availability |
| `HighP95Latency` | p95 request latency > 1s | performance |
| `ServiceUnavailable` | `up == 0` | availability |
| `DependencyFailureRate` | dependency call error rate > 20% | dependency |
| `CPUSaturation` | avg CPU ratio > 85% for 1m | saturation |
| `MemoryPressure` | resident memory > 200MB for 1m | saturation |

Thresholds and `for:` windows are deliberately low (a local demo
environment, not production tuning) so any chaos scenario reliably fires
its rule in well under two minutes -- see `simulator/scenarios.md` for
measured timings. `runbook_url` values point at a placeholder
`runbooks.example.com` domain; there is no real service there.

## Alertmanager payload mapping

Alertmanager's webhook payload shape is fixed by Alertmanager itself (see
its own docs) and cannot be made to match `IncomingAlertRequest`
directly, so `apps/api/routers/alerts.py` adds
`POST /api/v1/alerts/alertmanager` -- the same alert-ingestion component
(same router, same `IncidentCoreService`, same DB boundary), not a
parallel service, per `docs/architecture/13-security-boundaries.md`
placing "Alertmanager, PagerDuty, generic webhooks" at the same trust
boundary. Both routes converge on the same `_accept_alert` helper.

| Alertmanager webhook field | `AlertReceivedCommand` field | Notes |
|---|---|---|
| (always) | `source` | hardcoded `AlertSource.PROMETHEUS` -- this deployment's only Alertmanager origin |
| `alerts[i].fingerprint` | `external_id` | Alertmanager's own stable per-label-set dedup key, reused verbatim so our idempotency key inherits its stability |
| `alerts[i].labels` | `labels` | verbatim; must include `service`/`environment` (`AlertReceivedCommand`'s existing `REQUIRED_LABELS`) -- guaranteed by every rule in `service-alerts.yml` |
| `alerts[i].annotations` | `annotations` | verbatim (`summary`/`description`/`runbook_url`) |
| `alerts[i].labels.severity` | `severity` | validated against `AlertSeverity`; an alert rule without a recognized `severity` label 422s, same as the direct-POST path |
| `alerts[i].status` | `status` | `"firing"`/`"resolved"` match `AlertStatus`'s own values exactly, no translation needed |

A batch (`alerts[]`) becomes one `_accept_alert` call per alert, so
multi-alert correlation/dedup is exactly the same code path the
direct-POST endpoint uses -- see
`tests/e2e/test_alertmanager_webhook.py`.

**Auth**: `infrastructure/alertmanager/alertmanager.yml` presents a Bearer
token (`ALERTMANAGER_WEBHOOK_TOKEN`, default
`dev-local-alertmanager-token` -- change it via `.env` for anything beyond
local dev), checked by the route -- the "HMAC-signed / shared-secret
webhook auth" `13-security-boundaries.md` calls for at this trust
boundary. It's a shared secret, not a full HMAC signature scheme (a
future increment).

**Local-dev routing detail**: the API continues to run on the host via
`make run-api` (Phase 1/2's hot-reload workflow), not inside a container.
Alertmanager (containerized) reaches it via
`host.docker.internal:8000`, which requires the `extra_hosts:
host-gateway` entry on the `alertmanager` compose service -- see
ADR-0017.

## Chaos scenarios (`simulator/chaos/`)

Seven scenarios, started/stopped via `python -m simulator.chaos.cli`,
state held in Redis (`chaos:{service}`, self-expiring). See
`simulator/chaos/scenarios.py` for the full catalog and
`simulator/scenarios.md` for the incident scenarios built from them.

`high-cpu` and `memory-leak` are genuine, sustained resource consumption
(a busy-loop thread pool / a growing byte-string list) -- not faked
metric values, so `process_cpu_usage_ratio`/`process_memory_usage_bytes`
move for real. `high-latency`/`error-storm`/`dependency-failure`/
`bad-configuration` are real per-request effects (an actual `sleep`, an
actual 500 response), so the resulting latency/error-rate metrics are
genuine too.

**Do not hard-code root cause into the alert payload**: `error-storm` and
`bad-configuration` deliberately produce the *same* observable symptom
(elevated 5xx rate). Distinguishing "the service degraded" from "a bad
config value broke a code path" from telemetry alone is the future
investigation agent's job (Phase 5), not something this platform is
allowed to encode into the alert itself.

## Bugs caught during end-to-end verification

Running the actual stack end-to-end (not just unit/integration tests
against mocks) surfaced three real bugs, all fixed before this phase
shipped:

1. **`inventory-service`'s restock only rolled on a *successful*
   reservation.** Once a low-stock SKU (`GADGET-1`, seeded at 3 units)
   hit zero, every subsequent request 409'd before ever reaching the
   restock line, so it could never recover -- a permanent ~25% baseline
   error rate on 1 of 4 SKUs, with no chaos scenario active. Fixed by
   rolling the restock chance unconditionally, before the availability
   check.
2. **`checkout-service`/`payment-service` opened a new `httpx.AsyncClient`
   per request** instead of reusing one across the process lifetime. Under
   the load generator's sustained traffic this caused genuine intermittent
   `httpx.ConnectError`s (TCP connection churn), a second source of
   baseline noise indistinguishable from a real dependency problem. Fixed
   with a single shared client created at startup (`app.state.http_client`)
   and closed at shutdown.
3. **OTel's default histogram bucket boundaries are calibrated for
   millisecond-scale values**, but `http_request_duration_seconds`/
   `dependency_call_duration_seconds` record seconds -- so real ~10-25ms
   requests all landed in the first (0-5) bucket, and `histogram_quantile`
   in Prometheus interpolated a nonsensical multi-second p95 for traffic
   that was actually fast. Fixed with explicit, Prometheus-classic latency
   bucket boundaries in seconds (`explicit_bucket_boundaries_advisory` in
   `simulator/services/common/telemetry.py`).

None of these were caught by the payload-mapping tests
(`test_alertmanager_webhook.py`) or the real-container integration test
(`test_alertmanager_container.py`) -- both exercise the Alertmanager ->
API hop with synthetic, hand-built alerts, not the simulated services'
own generated telemetry under sustained load. They were only visible by
actually running `docker compose up` and watching real metrics.

## Phase 4 additions

- **Tempo** (above), and the evidence adapters that read Prometheus, Loki
  and Tempo -- `docs/architecture/08-evidence-model.md`, "Phase 4
  implementation".
- **Change registries** (`simulator/changes/`): the simulated CI/CD and
  config systems, as append-only Redis lists
  (`changes:deployments:{service}`, `changes:config:{service}`). Seeded by
  `make infra-up-full` (`python -m simulator.changes.cli seed`) with each
  service's actual deployed version and the latest commit touching its
  code. `bad-deployment` records a deploy on start and a rollback on stop;
  `bad-configuration` records a config push (`request_pipeline_config_version`
  v1 -> v2) and its revert. Records say *what* changed and who deployed it
  (`ci-pipeline` / `config-service`) -- never "chaos", never a cause.
  A scenario left to expire on its own records no rollback, because
  nothing rolled back.
- **Resolved alerts now drive the lifecycle** (ADR-0019): Alertmanager
  `external_id` is the firing episode (`fingerprint:startsAt`), and a
  resolution moves a `TRIAGING` incident to `CANCELLED` once no linked
  alert is firing.
- **Bug fixed**: `bad-deployment` never injected its `error_rate` --
  `ChaosController.should_fail()` omitted it, so the scenario flipped the
  version label but never fired `HighErrorRate`, contrary to scenario 1 in
  `simulator/scenarios.md`. Phase 3's demos used `error-storm`, so nothing
  caught it; Phase 4's live e2e test (`tests/e2e/test_evidence_live_incident.py`)
  did, on its first run.
- **Also caught during Phase 4's manual verification** (both fixed, both
  now covered by tests):
  - Tempo's search API returns *some* `limit` matches, not the newest --
    the trace adapter cut to the caller's limit before ordering, so an
    "errors" search during a live incident returned pre-incident traces.
    It now over-fetches up to 100 candidates, orders, then cuts.
  - The integration tests' "isolated" Redis fixture passed `db=15` to
    `Redis.from_url`, but the URL's own `/0` wins -- so each run flushed the
    live Redis (change registries, chaos state, Phase 2 event streams). The
    DB is now set in the URL, and the fixture refuses to flush anything but
    DB 15.

## Known limitations (Phase 3, updated in Phase 4)

- No real HMAC signing on the Alertmanager webhook, only a shared bearer
  token.
- `ServiceUnavailable`'s trigger isn't one of the 7 chaos-CLI scenarios
  (stopping a container isn't a per-request/in-process effect); it's
  demonstrated via `docker compose stop <service>` -- see
  `simulator/scenarios.md`.
- ~~No trace storage backend~~ -- fixed in Phase 4 (Tempo).
- ~~`RESOLVED`-status alerts don't drive any incident-status transition~~
  -- fixed in Phase 4 (ADR-0019).
- Trace search is "newest/slowest among up to 100 matches Tempo returns",
  not globally newest when more than 100 traces match in the window --
  a narrower window gets exact results.
- Single-binary Loki and Tempo report 503 on their ring-based `/ready`
  endpoints while serving queries normally; health checks (and
  `tests/conftest.py::stack_urls`) probe functional endpoints instead
  (`/loki/api/v1/labels`, `/api/echo`).
- The committed `simulator/services/*/Dockerfile`s are plain `pip
  install`-from-PyPI builds with no environment-specific workarounds. In
  the sandboxed environment this was built in, both Docker Hub image
  pulls and `pip install` from inside a build container occasionally
  needed retries / a proxy CA workaround documented in that environment's
  own `/root/.ccr/README.md` -- an artifact of that sandbox's network
  policy, not of this repository. A normal developer machine or CI runner
  builds these Dockerfiles as committed, with no changes.
