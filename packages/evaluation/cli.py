"""Evaluation and replay CLI.

    evaluate --scenario bad-deployment --mode fake            # offline, no credentials
    evaluate --all --mode fake --runs 3
    evaluate --scenario bad-deployment --mode live --runs 10 --yes
                                                              # configured provider/model;
                                                              # spends real tokens
    evaluate --list                                           # the golden scenarios

    replay --trace <recording-id | path>                      # inspect: no DB, model or telemetry
    replay --trace <recording-id | path> --verify             # re-execute deterministically
                                                              # (evaluation DB; no model/telemetry)
    replay --export <investigation-id>                        # record any investigation from
                                                              # the main DB (e.g. a live one)

Also: `python -m packages.evaluation {evaluate,replay} ...`, or the make
targets `eval`, `eval-live`, `replay`. Live mode is never the default and
never runs without `--yes`; the provider/model come from INVESTIGATION_PROVIDER
/ INVESTIGATION_MODEL (or --provider / --model) -- one configured model, no
model comparison.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic_settings import BaseSettings, SettingsConfigDict

from packages.agents.config import AnthropicCredentials, InvestigationSettings, ModelConfigError
from packages.agents.factory import ModelFactory, build_investigation_model, validate_provider
from packages.evaluation.harness import (
    FAKE_SPEC,
    RESULTS_DIR,
    EvalEnvironment,
    RunResult,
    fake_model_factory,
    live_spec,
    run_batch,
)
from packages.evaluation.recording import (
    TRACE_DIR,
    EvidenceStoreReader,
    build_recording,
    load_recording,
    save_recording,
)
from packages.evaluation.replay import render_timeline, signature, verify_replay
from packages.evaluation.scenario import load_scenarios
from packages.telemetry.logging import configure_logging


class EvalSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    eval_incident_core_database_url: str | None = None
    eval_evidence_database_url: str | None = None


def _environment() -> EvalEnvironment:
    settings = EvalSettings()
    if not settings.eval_incident_core_database_url or not settings.eval_evidence_database_url:
        raise SystemExit(
            "EVAL_INCIDENT_CORE_DATABASE_URL and EVAL_EVIDENCE_DATABASE_URL must point at the "
            "evaluation database (see .env.example; create it with `make eval-db`)"
        )
    try:
        return EvalEnvironment.from_urls(
            settings.eval_incident_core_database_url, settings.eval_evidence_database_url
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc


def _print_run(result: RunResult) -> None:
    g = result.grade
    verdict = "PASS" if g.passed else "FAIL"
    print(
        f"{verdict} {result.scenario_id:36} root_cause={g.root_cause['result']:12} "
        f"outcome={g.escalation['classification']}"
        f"{'/' + str(g.escalation['reason_code']) if g.escalation['reason_code'] else ''} "
        f"tools={g.process['tool_calls']} turns={g.process['model_turns']} "
        f"tokens={g.process['input_tokens']}/{g.process['output_tokens']} "
        f"cache_r={g.process['cache_read_tokens']} wall={g.process['wall_ms']}ms"
    )
    for failure in g.failures:
        print(f"     - {failure}")


def evaluate_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="evaluate", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--scenario", action="append", help="scenario id (repeatable)")
    target.add_argument("--all", action="store_true", help="every golden scenario")
    target.add_argument("--list", action="store_true", help="list the golden scenarios")
    parser.add_argument("--mode", choices=["fake", "live"], default="fake")
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--provider", help="live mode: overrides INVESTIGATION_PROVIDER")
    parser.add_argument("--model", help="live mode: overrides INVESTIGATION_MODEL")
    parser.add_argument("--yes", action="store_true", help="live mode: confirm real API calls")
    parser.add_argument("--results-dir", type=Path, default=RESULTS_DIR)
    parser.add_argument("--traces-dir", type=Path, default=TRACE_DIR)
    parser.add_argument("--json", action="store_true", help="print the summary as JSON")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    configure_logging("INFO" if args.verbose else "ERROR")

    scenarios = load_scenarios()
    if args.list:
        for s in scenarios.values():
            print(f"{s.id:36} {s.kind:9} {s.service:18} expects {s.expected.outcome}")
        return 0
    chosen = list(scenarios.values()) if args.all else []
    for sid in args.scenario or []:
        if sid not in scenarios:
            print(f"unknown scenario {sid!r}; try --list", file=sys.stderr)
            return 2
        chosen.append(scenarios[sid])
    if args.runs < 1:
        print("--runs must be >= 1", file=sys.stderr)
        return 2

    factory_for_run: Callable[[], ModelFactory] = fake_model_factory
    spec = FAKE_SPEC
    if args.mode == "live":
        overrides: dict[str, Any] = {}
        if args.provider:
            overrides["investigation_model_provider"] = args.provider
        if args.model:
            overrides["investigation_model"] = args.model
        try:
            spec = live_spec(InvestigationSettings(**overrides))
            validate_provider(spec.provider)
        except ModelConfigError as exc:
            print(f"live mode: {exc}", file=sys.stderr)
            return 2
        if spec.provider == "anthropic" and AnthropicCredentials().anthropic_api_key is None:
            print(
                "live mode needs provider credentials (ANTHROPIC_API_KEY for anthropic); "
                "--mode fake needs none",
                file=sys.stderr,
            )
            return 2
        calls = len(chosen) * args.runs
        print(
            f"live evaluation: {calls} investigation(s) on {spec.provider}/{spec.model} "
            f"(up to {InvestigationSettings().investigation_max_iterations} model calls each)"
        )
        if not args.yes:
            print("not started: pass --yes to make real, billed API calls", file=sys.stderr)
            return 2

        def factory_for_run() -> ModelFactory:
            return build_investigation_model

    env = _environment()
    _, summary = run_batch(
        chosen,
        env,
        runs=args.runs,
        mode=args.mode,
        spec=spec,
        model_factory_for_run=factory_for_run,
        results_dir=args.results_dir,
        traces_dir=args.traces_dir,
        on_result=None if args.json else _print_run,
    )
    overall = summary["overall"]
    if args.json:
        print(json.dumps(summary, indent=1, sort_keys=True))
    else:
        print(
            f"\n{overall['runs']} run(s): pass_rate={overall['pass_rate']} "
            f"root_cause_accuracy={overall['root_cause_accuracy']} "
            f"escalation_rate={overall['escalation_rate']} "
            f"correct_escalation_rate={overall['correct_escalation_rate']} "
            f"grounding_failures={overall['evidence_grounding_failures']} "
            f"unsafe={overall['unsafe_runs']} avg_tools={overall['avg_tool_calls']} "
            f"avg_turns={overall['avg_iterations']} avg_wall_ms={overall['avg_wall_ms']}"
        )
        print(f"results: {args.results_dir}/{summary['batch_id']}-summary.json")
    return 0 if overall["pass_rate"] == 1.0 else 1


def replay_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="replay", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--trace", help="recording id (under --traces-dir) or path")
    target.add_argument("--export", help="investigation id to record from the main database")
    parser.add_argument("--verify", action="store_true", help="re-execute and compare")
    parser.add_argument("--traces-dir", type=Path, default=TRACE_DIR)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    configure_logging("INFO" if args.verbose else "ERROR")

    if args.export:
        return _export(uuid.UUID(args.export), args.traces_dir)

    try:
        recording = load_recording(args.trace, args.traces_dir)
    except (OSError, ValueError) as exc:
        print(f"cannot load {args.trace}: {exc}", file=sys.stderr)
        return 2
    if not args.verify:
        if args.json:
            print(json.dumps(signature(recording), indent=1))
        else:
            print(render_timeline(recording))
        return 0

    env = _environment()
    env.reset()
    from packages.incident.investigations import InvestigationCoreService
    from packages.incident.service import IncidentCoreService

    report = verify_replay(
        recording,
        core=IncidentCoreService(env.incident_sessions),
        investigations=InvestigationCoreService(env.incident_sessions),
        evidence_reader=EvidenceStoreReader(env.evidence_sessions),
        catalog=env.catalog,
    )
    if args.json:
        print(
            json.dumps(
                {
                    "deterministic": report.deterministic,
                    "differences": report.differences,
                    "error": report.error,
                },
                indent=1,
            )
        )
    else:
        print(f"replay of {recording.recording_id}: deterministic={report.deterministic}")
        for d in report.differences:
            print(f"  - {d}")
        if report.error:
            print(f"  ! {report.error}")
    return 0 if report.deterministic else 1


def _export(investigation_id: uuid.UUID, traces_dir: Path) -> int:
    from packages.evidence.db.base import make_engine as make_evidence_engine
    from packages.evidence.db.base import make_session_factory as make_evidence_sessions
    from packages.incident.db.base import make_engine, make_session_factory
    from packages.incident.investigations import InvestigationCoreService
    from packages.incident.service import IncidentCoreService

    sessions = make_session_factory(make_engine(os.environ.get("INCIDENT_CORE_DATABASE_URL")))
    evidence = EvidenceStoreReader(
        make_evidence_sessions(make_evidence_engine(os.environ.get("EVIDENCE_DATABASE_URL")))
    )
    investigations = InvestigationCoreService(sessions)
    inv = investigations.load_state(investigation_id).investigation
    live = inv.model_provider not in ("fake", "replay")
    recording = build_recording(
        investigation_id,
        investigations=investigations,
        incidents=IncidentCoreService(sessions),
        evidence=evidence,
        model_mode="live" if live else "fake",
        evidence_mode="live",
    )
    path = save_recording(recording, traces_dir)
    print(f"recorded {investigation_id} -> {path} (redactions: {recording.redactions})")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] not in ("evaluate", "replay"):
        print("usage: python -m packages.evaluation {evaluate,replay} ...", file=sys.stderr)
        return 2
    command, rest = argv[0], argv[1:]
    return evaluate_main(rest) if command == "evaluate" else replay_main(rest)
