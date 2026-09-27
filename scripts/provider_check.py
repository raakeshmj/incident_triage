#!/usr/bin/env python3
"""Live connectivity check for the configured investigation provider --
tiny, cheap, and free of project data. No investigation runs.

    python scripts/provider_check.py            # uses INVESTIGATION_* from env / .env

1. catalog: GET <base_url>/models -- is INVESTIGATION_MODEL listed (exact id)?
2. completion: one minimal chat completion through the real adapter.
3. tool call: one request offering a trivial `add_numbers` tool; the
   response must normalize to a neutral action with parsed arguments.

The API key is read by the same code path as the worker and never printed.
Exit code 0 only if every step passed.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)
load_dotenv(ROOT / ".env")

from packages.agents.config import (  # noqa: E402
    PROVIDER_CREDENTIALS,
    InvestigationSettings,
    missing_credentials,
    resolve_model_spec,
)
from packages.agents.factory import build_investigation_model, validate_provider  # noqa: E402
from packages.agents.model import (  # noqa: E402
    ContextEntry,
    DecisionRequest,
    ModelError,
    ToolDefinition,
)

ADD = ToolDefinition(
    "add_numbers",
    "Add two integers and return the sum.",
    {
        "type": "object",
        "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
        "required": ["a", "b"],
        "additionalProperties": False,
    },
)


def _key(provider: str) -> str:
    settings_cls, field, _ = PROVIDER_CREDENTIALS[provider]
    return getattr(settings_cls(), field).get_secret_value()


def main() -> int:
    spec = resolve_model_spec(InvestigationSettings())
    validate_provider(spec.provider)
    if spec.base_url is None:
        print(f"{spec.provider} is not an OpenAI-compatible provider; nothing to check")
        return 2
    missing = missing_credentials(spec.provider)
    if missing:
        print(f"{missing} is not set (environment or .env)")
        return 2
    print(f"provider={spec.provider} base_url={spec.base_url} model={spec.model}")
    print(f"capabilities={json.dumps(spec.capabilities)}")
    ok = True

    # 1. catalog
    try:
        r = httpx.get(
            f"{spec.base_url}/models",
            headers={"Authorization": f"Bearer {_key(spec.provider)}"},
            timeout=30,
        )
        ids = [m.get("id") for m in r.json().get("data", []) if isinstance(m, dict)]
        listed = spec.model in ids
        near = [i for i in ids if spec.model.split("/")[-1].split(":")[0].lower() in str(i).lower()]
        print(f"[catalog] HTTP {r.status_code}: {len(ids)} models; exact id listed={listed}")
        if not listed:
            print(f"[catalog] similar ids: {near[:10]}")
            ok = False
        entry = next((m for m in r.json().get("data", []) if m.get("id") == spec.model), None)
        if entry:
            print(f"[catalog] entry: {json.dumps(entry)[:600]}")
    except (httpx.HTTPError, ValueError) as exc:
        print(f"[catalog] failed: {type(exc).__name__}")
        ok = False

    model = build_investigation_model(spec)

    # 2. minimal completion (no tools)
    try:
        turn = model.decide(
            DecisionRequest(
                "Answer with one word.", [], [ContextEntry("Reply with the single word: ready")]
            )
        )
        print(
            f"[completion] ok latency={turn.latency_ms}ms served={turn.served_model} "
            f"stop={turn.stop_reason} text={turn.text[:40]!r} usage={turn.usage}"
        )
    except ModelError as exc:
        print(f"[completion] {exc.code} retryable={exc.retryable}: {exc}")
        ok = False

    # 3. tool call
    try:
        turn = model.decide(
            DecisionRequest(
                "You must use the provided tool to answer.",
                [ADD],
                [ContextEntry("What is 17 + 25? Use the add_numbers tool.")],
            )
        )
        actions = [(a.name, a.arguments) for a in turn.actions]
        good = actions == [("add_numbers", {"a": 17, "b": 25})]
        print(
            f"[tool call] {'ok' if good else 'UNEXPECTED'} latency={turn.latency_ms}ms "
            f"stop={turn.stop_reason} actions={actions} usage={turn.usage} "
            f"cache_read={turn.cache.get('read_tokens')}"
        )
        ok = ok and good
    except ModelError as exc:
        print(f"[tool call] {exc.code} retryable={exc.retryable}: {exc}")
        ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
