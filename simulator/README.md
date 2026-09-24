# simulator

- `send_alert.py` -- Phase 1's synthetic alert CLI. POSTs directly to
  `POST /api/v1/alerts`, bypassing Prometheus/Alertmanager entirely --
  still the fastest way to sanity-check the API by hand.

  ```
  make send-alert
  python simulator/send_alert.py --service checkout --severity warning
  ```

## Phase 3: a realistic local production environment

See `docs/architecture/14-observability-and-chaos.md` for the full design
and `scenarios.md` for 6 worked incident scenarios.

- `services/common/` -- the shared OpenTelemetry/structlog/chaos runtime
  every simulated service uses (deliberately independent of
  `packages/telemetry` -- see ADR-0016 and the module's own docstring).
- `services/checkout-service/`, `services/payment-service/`,
  `services/inventory-service/` -- three simulated production services
  forming the chain `checkout -> payment -> inventory`, each a small
  FastAPI app plus its own Dockerfile.
- `services/load-generator/` -- continuous baseline traffic against
  `checkout-service` (5 req/s by default), so the alert rules'
  `rate()`-based expressions have something to evaluate.
- `chaos/scenarios.py` + `chaos/cli.py` -- the 7 chaos scenarios and the
  operator CLI to start/stop them:

  ```
  python -m simulator.chaos.cli list
  python -m simulator.chaos.cli start high-cpu --service checkout-service
  python -m simulator.chaos.cli status
  python -m simulator.chaos.cli stop --service checkout-service
  ```

- `scenarios.md` -- 6 documented incident scenarios (which chaos scenario
  to run, what telemetry it genuinely produces, which alert fires and
  roughly when).

More elaborate scenario generation (multi-alert storms, realistic
Alertmanager payload shapes for `send_alert.py` itself) is future work;
Phase 3 covers that ground through the real Alertmanager path instead.
