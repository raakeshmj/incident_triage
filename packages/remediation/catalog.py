"""The action catalog: the only vocabulary remediation can be expressed in
(ADR-0008).

Human-authored, code-reviewed, versioned reference data shipped with the
code (ADR-0024). A proposal names an entry by `action_id` and supplies
parameters that must validate against that entry's strict parameter model;
nothing -- no model output, no API caller -- can define a new action,
widen an entry, or add a parameter. The catalog version is recorded on
every remediation and policy decision.

Blast radius tiers: 1 = one service, reversible, no change to what is
deployed; 2 = changes what runs (a rollback / config revert) or adds
capacity beyond a small step; 3+ = never allowed by any current policy.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from packages.domain.verification import CheckTemplate, VerificationPolicy

CATALOG_VERSION = "catalog-2026.09-1"

_SERVICE = r"^[a-z0-9][a-z0-9-]{0,62}$"
_VERSION = r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$"
_KEY = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    service: str = Field(pattern=_SERVICE)


class RestartServiceParams(_Params):
    pass


class ScaleServiceParams(_Params):
    increase_by: int = Field(ge=1, le=5, description="Instances to add (bounded).")


class RollbackDeploymentParams(_Params):
    from_version: str = Field(pattern=_VERSION, description="Must be what is running now.")
    to_version: str = Field(pattern=_VERSION, description="The previously deployed version.")


class DisableFeatureFlagParams(_Params):
    flag: str = Field(pattern=_KEY)


class RevertConfigurationParams(_Params):
    key: str = Field(pattern=_KEY)
    from_value: str | int | float | bool | None = Field(description="Must be the current value.")
    to_value: str | int | float | bool | None

    @field_validator("to_value")
    @classmethod
    def _has_a_previous_value(cls, value: object) -> object:
        # A change with no previous value (a key that didn't exist) has nothing
        # to revert to; "reverting" it to null would be a new change.
        if value is None:
            raise ValueError("no previous value to revert to")
        return value


@dataclass(frozen=True)
class ActionCatalogEntry:
    action_id: str
    version: str
    description: str
    parameters: type[_Params]
    allowed_environments: frozenset[str]
    base_blast_radius: int
    max_blast_radius: int
    approval_mandatory: bool
    auto_execution_allowed: bool  # False for every entry: no auto path exists (Phase 7)
    timeout_seconds: int
    max_attempts: int  # executions, including retries
    retry_safe: bool  # may a failed/unknown attempt be retried without double-acting?
    requires_rca: bool
    target_must_match_root_cause: bool
    # What verification must observe afterwards (packages/domain/verification.py)
    verification: VerificationPolicy = field(
        default_factory=lambda: VerificationPolicy(0, 0, 1, 1, False)
    )

    def validate(self, parameters: dict[str, Any]) -> tuple[_Params | None, list[str]]:
        try:
            return self.parameters.model_validate(parameters), []
        except ValidationError as exc:
            return None, [
                f"{'.'.join(str(p) for p in e['loc']) or 'parameters'}: {e['msg']}"
                for e in exc.errors()
            ]

    def blast_radius(self, parameters: _Params) -> int:
        """Deterministic tier of this specific invocation."""
        tier = self.base_blast_radius
        if isinstance(parameters, ScaleServiceParams) and parameters.increase_by > 2:
            tier += 1
        return tier

    def describe(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "version": self.version,
            "description": self.description,
            "parameters_schema": self.parameters.model_json_schema(),
            "allowed_environments": sorted(self.allowed_environments),
            "base_blast_radius": self.base_blast_radius,
            "max_blast_radius": self.max_blast_radius,
            "approval_mandatory": self.approval_mandatory,
            "auto_execution_allowed": self.auto_execution_allowed,
            "timeout_seconds": self.timeout_seconds,
            "max_attempts": self.max_attempts,
            "retry_safe": self.retry_safe,
            "requires_rca": self.requires_rca,
            "target_must_match_root_cause": self.target_must_match_root_cause,
            "verification": self.verification.describe(),
        }


_ENVS = frozenset({"production", "staging"})
_HEALTHY = CheckTemplate("health.status")
_NO_NEW_ALERTS = CheckTemplate("alerts.no_new_firing")
_ERRORS_OK = CheckTemplate("metric.max", "error_rate", 0.05)
_LATENCY_OK = CheckTemplate("metric.max", "latency_p95", 1.0)

CATALOG: dict[str, ActionCatalogEntry] = {
    e.action_id: e
    for e in [
        ActionCatalogEntry(
            action_id="restart_service",
            version="1",
            description="Rolling restart of every instance of one service.",
            parameters=RestartServiceParams,
            allowed_environments=_ENVS,
            base_blast_radius=1,
            max_blast_radius=1,
            approval_mandatory=True,
            auto_execution_allowed=False,
            timeout_seconds=120,
            max_attempts=2,
            retry_safe=True,
            requires_rca=True,
            target_must_match_root_cause=True,
            verification=VerificationPolicy(
                45, 240, 15, 3, True, (_HEALTHY, _ERRORS_OK, _NO_NEW_ALERTS)
            ),
        ),
        ActionCatalogEntry(
            action_id="scale_service",
            version="1",
            description="Add a bounded number of instances to one service.",
            parameters=ScaleServiceParams,
            allowed_environments=_ENVS,
            base_blast_radius=1,
            max_blast_radius=2,
            approval_mandatory=True,
            auto_execution_allowed=False,
            timeout_seconds=180,
            max_attempts=1,
            retry_safe=False,  # a repeated "add N" adds 2N
            requires_rca=True,
            target_must_match_root_cause=True,
            verification=VerificationPolicy(
                30,
                240,
                15,
                3,
                True,
                (CheckTemplate("state.replicas"), _HEALTHY, _LATENCY_OK, _NO_NEW_ALERTS),
            ),
        ),
        ActionCatalogEntry(
            action_id="rollback_deployment",
            version="1",
            description="Redeploy the previously deployed version of one service.",
            parameters=RollbackDeploymentParams,
            allowed_environments=_ENVS,
            base_blast_radius=2,
            max_blast_radius=2,
            approval_mandatory=True,
            auto_execution_allowed=False,
            timeout_seconds=300,
            max_attempts=2,
            retry_safe=True,  # compare-and-set on from_version: a repeat is a no-op
            requires_rca=True,
            target_must_match_root_cause=True,
            verification=VerificationPolicy(
                60,
                300,
                15,
                3,
                True,
                (
                    CheckTemplate("state.deployment_version"),
                    _HEALTHY,
                    _ERRORS_OK,
                    _LATENCY_OK,
                    _NO_NEW_ALERTS,
                ),
            ),
        ),
        ActionCatalogEntry(
            action_id="disable_feature_flag",
            version="1",
            description="Turn one feature flag off for one service.",
            parameters=DisableFeatureFlagParams,
            allowed_environments=_ENVS,
            base_blast_radius=1,
            max_blast_radius=1,
            approval_mandatory=True,
            auto_execution_allowed=False,
            timeout_seconds=60,
            max_attempts=2,
            retry_safe=True,
            requires_rca=True,
            target_must_match_root_cause=True,
            verification=VerificationPolicy(
                30, 180, 15, 2, False, (CheckTemplate("state.flag"), _HEALTHY, _NO_NEW_ALERTS)
            ),
        ),
        ActionCatalogEntry(
            action_id="revert_configuration",
            version="1",
            description="Set one configuration key of one service back to its previous value.",
            parameters=RevertConfigurationParams,
            allowed_environments=_ENVS,
            base_blast_radius=2,
            max_blast_radius=2,
            approval_mandatory=True,
            auto_execution_allowed=False,
            timeout_seconds=120,
            max_attempts=2,
            retry_safe=True,  # compare-and-set on from_value
            requires_rca=True,
            target_must_match_root_cause=True,
            verification=VerificationPolicy(
                45,
                240,
                15,
                3,
                True,
                (CheckTemplate("state.config_value"), _HEALTHY, _ERRORS_OK, _NO_NEW_ALERTS),
            ),
        ),
    ]
}


def get_entry(action_id: str) -> ActionCatalogEntry | None:
    return CATALOG.get(action_id)


def catalog_digest() -> str:
    """Fingerprint of the catalog's reviewed content, recorded with decisions."""
    body = json.dumps([CATALOG[k].describe() for k in sorted(CATALOG)], sort_keys=True, default=str)
    return "sha256:" + hashlib.sha256(body.encode()).hexdigest()
