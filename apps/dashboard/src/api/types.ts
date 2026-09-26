// Typed API models: exactly what apps/api/routers/operations.py and
// remediations.py return. Statuses and verdicts arrive from the backend; the
// dashboard never derives them.

export type IncidentStatus =
  | "TRIAGING"
  | "INVESTIGATING"
  | "RCA_READY"
  | "AWAITING_APPROVAL"
  | "REMEDIATION_IN_PROGRESS"
  | "VERIFYING"
  | "VERIFICATION_FAILED"
  | "RESOLVED"
  | "ESCALATED"
  | "SUPPRESSED"
  | "CANCELLED"
  | "CLOSED";

export interface IncidentSummary {
  id: string;
  severity: string;
  service: string;
  environment: string;
  status: IncidentStatus;
  created_at: string;
  updated_at: string;
  ended_at: string | null;
  duration_seconds: number;
  alert_count: number;
  firing_alert_count: number;
  affected_services: string[];
  affected_service_count: number;
  attempt_count: number;
  investigation_status: string | null;
  remediation_status: string | null;
  verification_status: string | null;
}

export interface IncidentList {
  total: number;
  items: IncidentSummary[];
  services: string[];
}

export interface TimelineEvent {
  at: string;
  kind: "alert" | "state" | "investigation" | "evidence" | "hypothesis" | "rca" | "remediation" | "verification";
  title: string;
  detail?: string | null;
  ref?: string;
  evidence_ids?: string[];
}

export interface Alert {
  id: string;
  alertname: string | null;
  service: string | null;
  severity: string;
  status: string;
  source: string;
  summary: string | null;
  received_at: string;
  resolved_at: string | null;
}

export interface Hypothesis {
  key: string;
  description: string;
  cause_category: string | null;
  component: string | null;
  status: string;
  confidence: number | null;
  supporting_evidence_ids: string[];
  contradicting_evidence_ids: string[];
  missing_evidence: string[];
}

export interface ToolCall {
  sequence: number;
  iteration: number;
  at: string;
  tool: string;
  arguments: Record<string, unknown>;
  ok: boolean;
  error_code: string | null;
  summary: string | null;
  evidence_ids: string[];
}

export interface GroundedStatement {
  text: string;
  evidence_ids: string[];
}

export interface RcaReport {
  id: string;
  summary: string;
  report: {
    incident_summary: GroundedStatement;
    impact: GroundedStatement;
    root_cause: GroundedStatement;
    affected_services: { service: string; evidence_ids: string[] }[];
    contributing_factors?: GroundedStatement[];
    contradicting_evidence?: { evidence_id: string; explanation: string }[];
    unresolved_questions?: string[];
    recommended_next_diagnostic_action?: string | null;
    confidence: number;
    supporting_evidence: string[];
    root_cause_hypothesis: { key: string; description: string; cause_category: string | null; component: string | null };
  };
}

export interface InvestigationDetail {
  investigation: {
    id: string;
    attempt_number: number;
    status: string;
    model_provider: string;
    model_name: string;
    iteration_count: number;
    tool_call_count: number;
    evidence_count: number;
    escalation_reason: string | null;
    failure_reason: string | null;
    inconclusive_reason: string | null;
    started_at: string | null;
    completed_at: string | null;
  };
  hypotheses: Hypothesis[];
  tool_calls: ToolCall[];
  rca: RcaReport | null;
}

export interface Remediation {
  id: string;
  action_id: string;
  catalog_version: string;
  parameters: Record<string, unknown>;
  target_service: string;
  environment: string;
  reason: string;
  expected_effect: string;
  blast_radius_tier: number | null;
  proposal_hash: string;
  source: string;
  proposed_by: string;
  status: string;
  policy_decision_id: string | null;
  policy_decision: string | null;
  approval_status: string | null;
  execution_status: string | null;
  execution_attempts: number;
  failure_reason: string | null;
  verification_ref: string | null;
  created_at: string;
}

export interface PolicyDecision {
  id: string;
  decision: string;
  policy_version: string;
  catalog_version: string | null;
  blast_radius_tier: number | null;
  required_approver_roles: string[];
  rules: { rule_id: string; outcome: string; detail: string }[];
  reasons: string[];
  evaluated_at: string;
}

export interface RemediationTimelineEntry {
  sequence: number;
  event: string;
  from_status: string | null;
  to_status: string | null;
  actor: string;
  policy_version: string | null;
  occurred_at: string;
}

export interface RemediationDetail {
  remediation: Remediation;
  policy_decisions: PolicyDecision[];
  executions: { id: string; attempt: number; status: string; started_at: string; completed_at: string | null; error: string | null }[];
  timeline: RemediationTimelineEntry[];
  baseline: { values: Sample; evidence_ids: string[]; captured_at: string } | null;
}

export interface Sample {
  health?: { status: string; signals: Record<string, number | null> } | null;
  deployment?: { version: string | null } | null;
  config?: { effective: Record<string, unknown> } | null;
  runtime?: { replicas: number; flags: Record<string, string> } | null;
}

export interface CheckResult {
  kind: string;
  ok: boolean | null;
  observed: unknown;
  expected: unknown;
  detail: string;
}

export interface Observation {
  sequence: number;
  observed_at: string;
  passed: boolean;
  conclusive: boolean;
  checks: CheckResult[];
  errors: string[];
  evidence_ids: string[];
}

export interface VerificationDetail {
  verification: {
    id: string;
    status: "PENDING" | "RUNNING" | "PASSED" | "FAILED" | "TIMED_OUT";
    verification_type: string;
    policy_version: string;
    spec: {
      grace_seconds: number;
      window_seconds: number;
      poll_interval_seconds: number;
      timeout_seconds: number;
      required_consecutive: number;
      checks: { kind: string; metric?: string | null; max_value?: number | null; expected?: unknown }[];
    };
    baseline: (Sample & { evidence_ids?: string[] }) | null;
    consecutive_successes: number;
    observation_count: number;
    failure_reason: string | null;
    next_action: string | null;
    started_at: string | null;
    completed_at: string | null;
  };
  observations: Observation[];
  evidence: { evidence_id: string; poll_sequence: number; role: string; collected_at: string }[];
}

export interface IncidentDetail {
  incident: {
    id: string;
    status: IncidentStatus;
    severity: string;
    service: string;
    environment: string;
    attempt_count: number;
    created_at: string;
    updated_at: string;
    ended_at: string | null;
    duration_seconds: number;
  };
  alerts: Alert[];
  transitions: { at: string; from: string; to: string; reason: string }[];
  investigations: InvestigationDetail[];
  remediations: RemediationDetail[];
  verifications: VerificationDetail[];
  evidence: { id: string; evidence_type: string; source_system: string; collected_at: string; content_hash: string }[];
  timeline: TimelineEvent[];
}

export interface EvidenceDetail {
  evidence_id: string;
  incident_id: string;
  evidence_type: string;
  source_system: string;
  operation: string;
  subject_service: string;
  observed_at: string;
  collected_at: string;
  requested_by: string;
  content_hash: string;
  summary: string;
  query_spec: Record<string, unknown>;
  normalized_payload: Record<string, unknown>;
}

export interface MetricStat {
  count: number;
  unit?: string;
  p50?: number | null;
  p90?: number | null;
  mean?: number | null;
  max?: number | null;
  numerator?: number;
  rate?: number | null;
}

export type Metrics = Record<string, MetricStat>;

export interface Overview {
  incidents_by_status: Record<string, number>;
  active_incidents: number;
  investigations_running: number;
  awaiting_approval: number;
  remediations_executing: number;
  verifications_running: number;
  recent_escalations: { incident_id: string; at: string; reason: string }[];
  outbox_failed_events: number;
  outbox_unpublished: number;
  dead_letter_count: number | null;
  workers: { role: string; key: string; age_seconds: number }[];
  redis_available: boolean;
}

export interface IncidentFilters {
  status?: string;
  severity?: string;
  service?: string;
  range?: string; // "1h" | "24h" | "7d" | "all"
}
