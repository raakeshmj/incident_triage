# Incident scenarios

Six scenarios combining the chaos engine (`simulator/chaos/`), the
telemetry it genuinely produces, and the Prometheus alert rules
(`infrastructure/prometheus/alerts/service-alerts.yml`) that fire from
it. Every alert Incident Intelligence receives here carries only
symptoms (error rate, latency, memory, CPU, dependency failure) --
never a root cause -- per the Phase 3 brief; matching a scenario to its
underlying chaos scenario is something a human (or, later, the
investigation agent) does by reading the telemetry, not something the
alert payload tells them.

Expected timings below are derived from each alert's `for:` duration plus
Prometheus's 5s scrape/evaluation interval and Alertmanager's 5s
`group_wait` -- i.e. roughly `for: duration + ~10-15s`.

**Measured, on real containers, from a clean `docker compose down -v` /
`up -d` state** (see `docs/architecture/14-observability-and-chaos.md`'s
verification log):

| Scenario | Chaos start -> alert firing | Alert firing -> incident created | Chaos stop -> alert resolved |
|---|---|---|---|
| `error-storm` on checkout-service (scenario "error-storm and bad-configuration", `HighErrorRate`) | 43s | <1s | 53s |
| `memory-leak` on inventory-service (scenario 4, `MemoryPressure`) | 95s | <1s | 8s |

Both measurements are end-to-end through the real stack: real Prometheus
evaluating real scraped metrics, real Alertmanager grouping/delivering,
the real running API creating the real Postgres row.

## 1. Bad deployment to checkout-service

```
python -m simulator.chaos.cli start bad-deployment --service checkout-service
```

**What happens**: checkout-service's error rate rises to ~30% and its
`service_deployment_info` metric flips to a new version label at the same
moment -- a real rollout of a broken build landing while the load
generator's traffic is in flight.

**What Incident Intelligence sees**: a `HighErrorRate` alert for
`checkout-service`/`production`, ~40-45s after the chaos scenario starts.
One new `TRIAGING` incident, `service=checkout-service`. Nothing in the
alert payload mentions a deployment -- that correlation (an error-rate
alert landing at the same instant as a `service_deployment_info` version
change) is exactly the kind of fact a future investigation agent would
have to go find in the metrics itself.

**Stop**: `python -m simulator.chaos.cli stop --service checkout-service`.

## 2. Runaway CPU on payment-service

```
python -m simulator.chaos.cli start high-cpu --service payment-service
```

**What happens**: payment-service spins two real busy-loop threads,
genuinely saturating its CPU. Because CPython's GIL means those threads
compete with the request-handling event loop, request latency on
payment-service typically degrades too -- a realistic (not scripted)
secondary symptom.

**What Incident Intelligence sees**: a `CPUSaturation` alert for
`payment-service` (~70-75s, `for: 1m`), and possibly a `HighP95Latency`
alert alongside it depending on how much the GIL contention actually
slows request handling -- another case where two different alerts can
describe the same underlying event.

**Stop**: `python -m simulator.chaos.cli stop --service payment-service`.

## 3. Inventory outage cascades through the whole chain

```
python -m simulator.chaos.cli start dependency-failure --service inventory-service
```

**What happens**: inventory-service starts failing/timing out on nearly
every request. payment-service's calls to it fail, so payment-service
itself returns 502s to checkout-service, which in turn returns 502s to
the load generator -- a single failure, three services' error-rate
metrics moving.

**What Incident Intelligence sees**: potentially **three separate,
concurrent incidents** -- one per service (the correlation engine
correlates by `service`+`environment`, not across services, so it never
invents a cross-service link it can't justify): `HighErrorRate` on
`inventory-service` itself, `DependencyFailureRate` on `payment-service`
(its calls to inventory-service failing) plus `HighErrorRate` on
`payment-service` (its own 502s to checkout-service), and `HighErrorRate`
on `checkout-service`. All landing within the same ~30-45s window --
exactly the kind of "three incidents that are actually one story" a human
(or a future cross-incident-aware agent) would need to link by hand.
This is deliberate: Phase 3 has no cross-service root-cause capability,
by design.

**Stop**: `python -m simulator.chaos.cli stop --service inventory-service`.

## 4. Slow memory leak in inventory-service

```
python -m simulator.chaos.cli start memory-leak --service inventory-service --duration 180
```

**What happens**: inventory-service grows an in-process buffer by ~5MB/s.
No request behavior changes -- customers see nothing wrong yet.

**What Incident Intelligence sees**: a `MemoryPressure` alert once
resident memory holds above 200MB for a full minute (typically ~55-90s
after starting, depending on the process's baseline RSS) -- Incident
Intelligence flags a problem *before* it becomes a customer-visible
outage, exactly the value case for a saturation-class alert.

**Stop**: `python -m simulator.chaos.cli stop --service inventory-service`
(releases the buffer immediately; the metric drops back down within one
scrape interval).

## 5. Gradual latency degradation on checkout-service

```
python -m simulator.chaos.cli start high-latency --service checkout-service --params '{"delay_seconds": 2.5}'
```

**What happens**: checkout-service sleeps ~2.5s before responding to every
request. No errors -- every request still succeeds, just slowly.

**What Incident Intelligence sees**: a `HighP95Latency` alert (~40-45s)
for `checkout-service`, with `HighErrorRate` staying quiet throughout --
demonstrates the platform distinguishing a pure performance degradation
from an availability incident using the same alert taxonomy.

**Stop**: `python -m simulator.chaos.cli stop --service checkout-service`.

## 6. Total service outage (not a chaos-CLI scenario)

```
docker compose stop checkout-service
```

**What happens**: Prometheus can no longer scrape checkout-service at
all -- a full outage, not a degraded-but-responding service.

**What Incident Intelligence sees**: `ServiceUnavailable` (`up == 0`),
firing after 15s -- the fastest of all six alerts, since a target simply
disappearing needs no rate-based math. This is the one scenario not
listed among the 7 `simulator/chaos/scenarios.py` entries: it's an
infrastructure-level failure (the process is gone), not an in-process
effect a running service can inject on itself.

**Recover**: `docker compose start checkout-service`.

## error-storm and bad-configuration

Not written up as separate scenarios above because their *observable*
symptom is identical to scenario 1's `HighErrorRate` by design (see
`simulator/chaos/scenarios.py`'s module docstring) -- run either the same
way (`chaos start error-storm --service <svc>` /
`chaos start bad-configuration --service <svc>`) to see the same alert
fire from a different (equally plausible) underlying cause.
