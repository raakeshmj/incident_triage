"""Import-level enforcement of the Phase 4 security boundary
(docs/architecture/13-security-boundaries.md):

    future agent -> tool layer -> evidence service -> read-only telemetry

- packages/tools reaches telemetry only through packages.evidence.service:
  no HTTP/Redis/DB/subprocess clients, no adapters, no incident-core.
- packages/evidence never touches incident-core's tables -- only its read
  API/command via the IncidentGateway protocol.
- alert-ingestion (apps/api/routers/alerts.py) still has no DB access.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _imports(package: str) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for path in (ROOT / package).rglob("*.py"):
        names: set[str] = set()
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module)
        found[str(path.relative_to(ROOT))] = names
    return found


def _violations(package: str, forbidden: tuple[str, ...]) -> list[str]:
    return [
        f"{file} imports {name}"
        for file, names in _imports(package).items()
        for name in names
        if name.startswith(forbidden)
    ]


def test_tool_layer_has_no_path_to_backends_or_databases():
    forbidden = (
        "httpx",
        "redis",
        "sqlalchemy",
        "subprocess",
        "psycopg",
        "packages.incident",
        "packages.evidence.adapters",
        "packages.evidence.db",
        "packages.evidence.repository",
    )
    assert _violations("packages/tools", forbidden) == []


def test_evidence_service_never_touches_incident_core_storage():
    assert _violations("packages/evidence", ("packages.incident",)) == []


def test_alert_ingestion_still_has_no_database_access():
    names = _imports("apps/api/routers")["apps/api/routers/alerts.py"]
    assert not {n for n in names if n.startswith(("packages.incident.db", "sqlalchemy"))}


def test_investigation_agent_has_no_path_to_state_backends_or_shell():
    """The agent package reaches incident state only through incident-core's
    command interface and telemetry only through the tool layer."""
    forbidden = (
        "sqlalchemy",
        "psycopg",
        "redis",
        "httpx",
        "subprocess",
        "packages.incident",
        "packages.evidence.adapters",
        "packages.evidence.db",
        "packages.evidence.repository",
    )
    assert _violations("packages/agents", forbidden) == []


def test_only_the_claude_adapter_imports_the_model_sdk():
    importers = sorted(
        file
        for package in ("apps", "packages")
        for file, names in _imports(package).items()
        if any(n == "anthropic" or n.startswith("anthropic.") for n in names)
    )
    assert importers == ["packages/agents/claude.py"]
