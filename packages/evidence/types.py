"""Evidence type and source-system vocabularies.

Type names follow the Phase 4 brief; docs/architecture/08-evidence-model.md
maps them to the earlier sketch's names (`deploy` -> `deployment`,
`git_diff` -> `git_change`, `config_change` -> `configuration`,
`historical_incident` -> `incident_history`).
"""

from __future__ import annotations

from enum import Enum


class EvidenceType(str, Enum):
    METRIC = "metric"
    LOG = "log"
    TRACE = "trace"
    DEPLOYMENT = "deployment"
    GIT_CHANGE = "git_change"
    CONFIGURATION = "configuration"
    INCIDENT_HISTORY = "incident_history"


class SourceSystem(str, Enum):
    PROMETHEUS = "prometheus"
    LOKI = "loki"
    TEMPO = "tempo"
    DEPLOYMENT_REGISTRY = "deployment-registry"
    CONFIG_REGISTRY = "config-registry"
    GIT = "git"
    INCIDENT_CORE = "incident-core"
    RUNTIME_REGISTRY = "runtime-registry"


# Time-sensitive observations get an `expires_at` (08-evidence-model.md,
# "Confidence, staleness, and contradiction"): after this, a citation is
# still resolvable but visibly stale relative to "now". Change records
# (deployments, commits, config) and incident history don't go stale the
# same way -- what happened, happened -- so they don't expire.
EXPIRING_TYPES = frozenset({EvidenceType.METRIC, EvidenceType.LOG, EvidenceType.TRACE})
EVIDENCE_TTL_DAYS = 30
