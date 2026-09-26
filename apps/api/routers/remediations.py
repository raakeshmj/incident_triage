"""Operator endpoints for Phase 7 remediation: read the remediation and its
audit trail, approve or reject it, propose or cancel one, and flip kill
switches.

Authentication: `Authorization: Bearer <OPERATOR_API_TOKEN>`; disabled
entirely when no token is configured. The approver named in a decision must
be on the configured roster, and their roles come from the roster -- never
from the request. An approval must carry the `proposal_hash` and
`policy_decision_id` the approver reviewed; anything else is refused.
Nothing here can override a policy rejection or execute an action directly:
approval only makes a remediation eligible for the runner.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from apps.api.config import get_settings
from apps.api.dependencies import get_remediation_service
from packages.domain.errors import (
    ApprovalMismatchError,
    ApproverNotAuthorizedError,
    IncidentNotFoundError,
    RemediationNotFoundError,
    RemediationStateError,
)
from packages.domain.remediation import RemediationProposal, RemediationView
from packages.incident.remediations import RemediationCoreService
from packages.remediation.catalog import CATALOG, CATALOG_VERSION
from packages.telemetry.logging import get_logger

log = get_logger(__name__)
router = APIRouter(prefix="/api/v1", tags=["remediation"])


def require_operator(authorization: str | None = Header(default=None)) -> None:
    token = get_settings().operator_api_token
    if not token:
        raise HTTPException(status_code=403, detail="operator endpoints are disabled")
    if authorization != f"Bearer {token}":
        log.warning("remediation_api.unauthorized")
        raise HTTPException(status_code=401, detail="invalid or missing operator credentials")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ApprovalRequest(_Strict):
    approver: str = Field(min_length=1, max_length=128)
    decision: Literal["approve", "reject"]
    proposal_hash: str
    policy_decision_id: uuid.UUID
    comment: str | None = Field(default=None, max_length=1000)


class OperatorProposalRequest(_Strict):
    action_id: str
    parameters: dict[str, Any]
    reason: str = Field(min_length=1, max_length=1000)
    expected_effect: str = Field(min_length=1, max_length=1000)
    proposed_by: str = Field(min_length=1, max_length=128)
    idempotency_key: str = Field(min_length=1, max_length=200)
    investigation_id: uuid.UUID | None = None


class CancelRequest(_Strict):
    actor: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=1000)


class KillSwitchRequest(_Strict):
    engaged: bool
    actor: str = Field(min_length=1, max_length=128)
    reason: str | None = Field(default=None, max_length=1000)


def _errors(exc: Exception) -> HTTPException:
    if isinstance(exc, (RemediationNotFoundError, IncidentNotFoundError)):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, ApproverNotAuthorizedError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, (ApprovalMismatchError, RemediationStateError)):
        return HTTPException(status_code=409, detail=str(exc))
    raise exc


@router.get("/action-catalog")
def action_catalog() -> dict[str, Any]:
    return {"version": CATALOG_VERSION, "actions": [e.describe() for e in CATALOG.values()]}


@router.get("/incidents/{incident_id}/remediations", response_model=list[RemediationView])
def list_remediations(
    incident_id: uuid.UUID, service: RemediationCoreService = Depends(get_remediation_service)
) -> list[RemediationView]:
    return service.list_for_incident(incident_id)


@router.get("/remediations/{remediation_id}")
def get_remediation(
    remediation_id: uuid.UUID, service: RemediationCoreService = Depends(get_remediation_service)
) -> dict[str, Any]:
    try:
        view = service.get(remediation_id)
    except RemediationNotFoundError as exc:
        raise _errors(exc) from exc
    return {
        "remediation": view.model_dump(mode="json"),
        "policy_decisions": service.policy_decisions(remediation_id),
        "executions": service.executions(remediation_id),
        "timeline": service.timeline(remediation_id),
    }


@router.post(
    "/remediations/{remediation_id}/approval",
    response_model=RemediationView,
    dependencies=[Depends(require_operator)],
)
def decide(
    remediation_id: uuid.UUID,
    body: ApprovalRequest,
    service: RemediationCoreService = Depends(get_remediation_service),
) -> RemediationView:
    roster = get_settings().approver_roles()
    if body.approver not in roster:
        raise HTTPException(status_code=403, detail=f"{body.approver} is not an approver")
    try:
        return service.decide_approval(
            remediation_id,
            approver=body.approver,
            approver_roles=roster[body.approver],
            approve=body.decision == "approve",
            proposal_hash=body.proposal_hash,
            policy_decision_id=body.policy_decision_id,
            comment=body.comment,
        )
    except Exception as exc:
        raise _errors(exc) from exc


@router.post(
    "/incidents/{incident_id}/remediations",
    response_model=RemediationView,
    status_code=201,
    dependencies=[Depends(require_operator)],
)
def propose(
    incident_id: uuid.UUID,
    body: OperatorProposalRequest,
    service: RemediationCoreService = Depends(get_remediation_service),
) -> RemediationView:
    try:
        proposal = RemediationProposal(
            incident_id=incident_id,
            investigation_id=body.investigation_id,
            action_id=body.action_id,
            parameters=body.parameters,
            reason=body.reason,
            expected_effect=body.expected_effect,
            source="operator",
            proposed_by=body.proposed_by,
        )
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        return service.propose(proposal, idempotency_key=f"operator:{body.idempotency_key}")
    except Exception as exc:
        raise _errors(exc) from exc


@router.post(
    "/remediations/{remediation_id}/cancel",
    response_model=RemediationView,
    dependencies=[Depends(require_operator)],
)
def cancel(
    remediation_id: uuid.UUID,
    body: CancelRequest,
    service: RemediationCoreService = Depends(get_remediation_service),
) -> RemediationView:
    try:
        return service.cancel(remediation_id, actor=f"human:{body.actor}", reason=body.reason)
    except Exception as exc:
        raise _errors(exc) from exc


@router.put("/kill-switches/{scope}", dependencies=[Depends(require_operator)])
def set_kill_switch(
    scope: str,
    body: KillSwitchRequest,
    service: RemediationCoreService = Depends(get_remediation_service),
) -> dict[str, bool]:
    try:
        service.set_kill_switch(
            scope, engaged=body.engaged, actor=f"human:{body.actor}", reason=body.reason
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return service.kill_switches()


@router.get("/kill-switches")
def kill_switches(
    service: RemediationCoreService = Depends(get_remediation_service),
) -> dict[str, bool]:
    return service.kill_switches()
