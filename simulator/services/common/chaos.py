"""Chaos-state reader + effect engine, running inside each simulated service.

Scenario state lives in Redis (key `chaos:{service_name}`, a JSON blob
written by `simulator/chaos/cli.py`), not in the service's own memory --
so an operator can start/stop chaos from outside the container and every
replica (were there more than one) would see the same state. Each service
polls it in a background thread (see `ChaosController._poll_loop`) and
applies effects:

- **high-cpu** / **memory-leak**: genuine, sustained resource consumption
  (a busy-loop thread pool / a growing byte-string list) -- these are not
  faked metrics, they make the process actually burn CPU / hold memory, so
  `process_cpu_usage_ratio` / `process_memory_usage_bytes` (telemetry.py)
  reflect a real symptom.
- **high-latency** / **error-storm** / **dependency-failure** /
  **bad-configuration**: per-request effects (`extra_latency()` /
  `should_fail()`), applied by each service's own request handlers so the
  resulting elevated latency/error-rate metrics are also genuine, not
  injected directly into Prometheus.
- **bad-deployment**: combines an error-rate bump with a
  `service_deployment_info` version-label change (`deployment_override()`)
  -- see `docs/architecture/14-observability-and-chaos.md`.

See `simulator/chaos/scenarios.py` for the full catalog and default
parameters, and note there on why `error-storm` and `bad-configuration`
deliberately produce the *same* observable symptom (elevated 5xx rate):
the platform must correlate on symptoms, never a hard-coded root cause.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any

import redis

from simulator.services.common.telemetry import get_logger

log = get_logger("chaos")

_POLL_INTERVAL_SECONDS = 1.0


class ChaosController:
    def __init__(self, redis_url: str, service_name: str) -> None:
        self._redis = redis.Redis.from_url(redis_url, decode_responses=True)
        self._service_name = service_name
        self._lock = threading.Lock()
        self._state: dict[str, Any] | None = None

        self._cpu_stop = threading.Event()
        self._cpu_threads: list[threading.Thread] = []

        self._memory_stop = threading.Event()
        self._memory_thread: threading.Thread | None = None
        self._memory_ballast: list[bytes] = []

    def start(self) -> None:
        threading.Thread(target=self._poll_loop, daemon=True).start()

    def _poll_loop(self) -> None:
        while True:
            try:
                self._refresh()
            except Exception:  # local dev chaos plumbing must never crash the service
                log.warning("chaos.poll_failed", exc_info=True)
            time.sleep(_POLL_INTERVAL_SECONDS)

    def _refresh(self) -> None:
        raw = self._redis.get(f"chaos:{self._service_name}")
        state = json.loads(raw) if raw else None
        if state and state.get("expires_at") is not None and time.time() > state["expires_at"]:
            state = None

        with self._lock:
            changed = state != self._state
            self._state = state

        if changed:
            log.info("chaos.state_changed", scenario=(state or {}).get("scenario"))
            self._apply_side_effects(state)

    def current(self) -> dict[str, Any] | None:
        with self._lock:
            return self._state

    # -- per-request effects -------------------------------------------------

    def should_fail(self) -> bool:
        import random

        state = self.current()
        # bad-deployment belongs here too: a broken build *is* the errors.
        # (Phase 3 omitted it, so that scenario only flipped the version
        # label and never fired HighErrorRate -- caught by Phase 4's live
        # e2e test, tests/e2e/test_evidence_live_incident.py.)
        error_scenarios = (
            "error-storm",
            "bad-configuration",
            "dependency-failure",
            "bad-deployment",
        )
        if not state or state["scenario"] not in error_scenarios:
            return False
        return random.random() < float(state["params"].get("error_rate", 0.5))

    def extra_latency_seconds(self) -> float:
        state = self.current()
        if not state:
            return 0.0
        if state["scenario"] == "high-latency":
            return float(state["params"].get("delay_seconds", 2.0))
        if state["scenario"] == "dependency-failure":
            return float(state["params"].get("timeout_seconds", 0.0))
        return 0.0

    def deployment_override(self) -> dict[str, Any] | None:
        state = self.current()
        if state and state["scenario"] == "bad-deployment":
            return state["params"]
        return None

    # -- sustained resource-consumption effects ------------------------------

    def _apply_side_effects(self, state: dict[str, Any] | None) -> None:
        scenario = state["scenario"] if state else None

        if scenario == "high-cpu":
            self._start_cpu_burn(int((state or {}).get("params", {}).get("thread_count", 2)))
        else:
            self._stop_cpu_burn()

        if scenario == "memory-leak":
            self._start_memory_leak(int((state or {}).get("params", {}).get("mb_per_second", 5)))
        else:
            self._stop_memory_leak()

    def _start_cpu_burn(self, thread_count: int) -> None:
        if self._cpu_threads:
            return
        self._cpu_stop.clear()

        def burn() -> None:
            while not self._cpu_stop.is_set():
                _ = sum(i * i for i in range(50_000))

        for _ in range(thread_count):
            thread = threading.Thread(target=burn, daemon=True)
            thread.start()
            self._cpu_threads.append(thread)

    def _stop_cpu_burn(self) -> None:
        if not self._cpu_threads:
            return
        self._cpu_stop.set()
        self._cpu_threads = []

    def _start_memory_leak(self, mb_per_second: int) -> None:
        if self._memory_thread is not None:
            return
        self._memory_stop.clear()

        def grow() -> None:
            while not self._memory_stop.is_set():
                self._memory_ballast.append(b"x" * (mb_per_second * 1024 * 1024))
                time.sleep(1.0)

        self._memory_thread = threading.Thread(target=grow, daemon=True)
        self._memory_thread.start()

    def _stop_memory_leak(self) -> None:
        if self._memory_thread is None:
            return
        self._memory_stop.set()
        self._memory_thread = None
        self._memory_ballast.clear()
