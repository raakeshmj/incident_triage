import {
  Button,
  InlineNotification,
  Modal,
  PasswordInput,
  ProgressIndicator,
  ProgressStep,
  RadioButton,
  RadioButtonGroup,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
  TextArea,
  TextInput,
  Toggle,
} from "@carbon/react";
import { useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { ApiError, api } from "../api/client";
import { clock, dateTime, duration, label, LIFECYCLE, metricValue, shortId } from "../api/format";
import type {
  CheckResult,
  IncidentDetail,
  InvestigationDetail,
  RemediationDetail,
  Sample,
  TimelineEvent,
  VerificationDetail,
} from "../api/types";
import { Empty, EvidenceLinks, EvidenceModal, Loading, LoadError, Panel, ScrollRegion, StatusTag, useLoad } from "../components/common";
import { useMedia } from "../components/useMedia";

const FINAL = new Set(["RESOLVED", "ESCALATED", "CANCELLED", "CLOSED", "SUPPRESSED"]);

export function IncidentDetailPage() {
  const { id = "" } = useParams();
  const [evidence, setEvidence] = useState<string | null>(null);
  const { data, error, loading, refresh } = useLoad(() => api.incident(id), [id], 5000);

  if (loading && !data) {
    return (
      <div className="ii-page">
        <Loading lines={8} />
      </div>
    );
  }
  if (error && !data) {
    return (
      <div className="ii-page">
        <Breadcrumb id={id} />
        <LoadError error={error} onRetry={refresh} />
      </div>
    );
  }
  if (!data) return null;
  return (
    <div className="ii-page">
      <Header detail={data} />
      <Lifecycle detail={data} />
      <ActionNeeded detail={data} onChanged={refresh} />
      <div className="ii-detail-grid">
        <div className="ii-col">
          <Timeline events={data.timeline} onEvidence={setEvidence} />
          <Impact detail={data} />
        </div>
        <div className="ii-col">
          <Rca detail={data} onEvidence={setEvidence} />
          <Investigation investigations={data.investigations} onEvidence={setEvidence} />
          <Remediations remediations={data.remediations} />
          <Verifications verifications={data.verifications} onEvidence={setEvidence} />
        </div>
      </div>
      <EvidenceModal id={evidence} onClose={() => setEvidence(null)} />
    </div>
  );
}

function Breadcrumb({ id }: { id: string }) {
  return (
    <nav aria-label="Breadcrumb" className="ii-breadcrumb ii-mono">
      <Link to="/">Incidents</Link> / {shortId(id)}
    </nav>
  );
}

function Header({ detail }: { detail: IncidentDetail }) {
  const i = detail.incident;
  return (
    <header className="ii-detail-head">
      <Breadcrumb id={i.id} />
      <div className="ii-detail-title">
        <h1 className="ii-title">
          {i.service} <span className="ii-subtle">· {i.environment}</span>
        </h1>
        <StatusTag value={i.status} />
      </div>
      <dl className="ii-facts">
        <div>
          <dt>Severity</dt>
          <dd>
            <StatusTag value={i.severity} />
          </dd>
        </div>
        <div>
          <dt>Opened</dt>
          <dd className="ii-mono">{dateTime(i.created_at)}</dd>
        </div>
        <div>
          <dt>{FINAL.has(i.status) ? "Duration" : "Open for"}</dt>
          <dd className="ii-mono">{duration(i.duration_seconds)}</dd>
        </div>
        <div>
          <dt>Investigation attempts</dt>
          <dd>{i.attempt_count}</dd>
        </div>
        <div>
          <dt>Incident id</dt>
          <dd className="ii-mono ii-break">{i.id}</dd>
        </div>
      </dl>
    </header>
  );
}

function Lifecycle({ detail }: { detail: IncidentDetail }) {
  const narrow = useMedia("(max-width: 671px)");
  const status = detail.incident.status;
  const keys: string[] = LIFECYCLE.map((s) => s.key);
  let current = keys.indexOf(status);
  let stoppedAt = -1;
  if (current < 0) {
    // ended off the happy path: mark the step it left from
    const last = [...detail.transitions].reverse().find((t) => t.to === status);
    stoppedAt = last ? keys.indexOf(last.from === "VERIFICATION_FAILED" ? "VERIFYING" : last.from) : 0;
    current = Math.max(stoppedAt, 0);
  }
  if (narrow) {
    const step = LIFECYCLE[current];
    return (
      <p className="ii-lifecycle-compact">
        Lifecycle: <b>{stoppedAt >= 0 ? `${label(status)} at ${step.label}` : step.label}</b>{" "}
        <span className="ii-muted">
          (step {current + 1} of {LIFECYCLE.length})
        </span>
      </p>
    );
  }
  return (
    <div className="ii-lifecycle" aria-label="Lifecycle">
      <ProgressIndicator currentIndex={current} spaceEqually>
        {LIFECYCLE.map((step, index) => (
          <ProgressStep
            key={step.key}
            label={step.label}
            complete={index < current || (status === "RESOLVED" && index === current)}
            current={index === current && status !== "RESOLVED"}
            invalid={index === stoppedAt}
            secondaryLabel={index === stoppedAt ? label(status) : undefined}
            description={step.label}
          />
        ))}
      </ProgressIndicator>
    </div>
  );
}

// --- approval ----------------------------------------------------------------------

const TOKEN_KEY = "ii.operatorToken";

function ActionNeeded({ detail, onChanged }: { detail: IncidentDetail; onChanged: () => void }) {
  const pending = detail.remediations.find((r) => r.remediation.status === "AWAITING_APPROVAL");
  const [open, setOpen] = useState(false);
  if (!pending) return null;
  const r = pending.remediation;
  const decision = pending.policy_decisions[0];
  return (
    <section className="ii-action" aria-labelledby="action-title">
      <div>
        <h2 id="action-title">Approval needed</h2>
        <p>
          <span className="ii-mono">{r.action_id}</span> on <b>{r.target_service}</b> · blast radius {r.blast_radius_tier ?? "—"} · requires{" "}
          {decision?.required_approver_roles.join(" or ") || "an approver"}
        </p>
      </div>
      <Button kind="primary" size="md" onClick={() => setOpen(true)}>
        Review and decide
      </Button>
      <ApprovalModal open={open} detail={pending} onClose={() => setOpen(false)} onDone={onChanged} />
    </section>
  );
}

function ApprovalModal({ open, detail, onClose, onDone }: { open: boolean; detail: RemediationDetail; onClose: () => void; onDone: () => void }) {
  const r = detail.remediation;
  const [token, setToken] = useState(() => {
    try {
      return sessionStorage.getItem(TOKEN_KEY) ?? "";
    } catch {
      return "";
    }
  });
  const [approver, setApprover] = useState("");
  const [choice, setChoice] = useState<"approve" | "reject">("approve");
  const [comment, setComment] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      await api.decide(r.id, token, {
        approver,
        decision: choice,
        proposal_hash: r.proposal_hash,
        policy_decision_id: r.policy_decision_id ?? "",
        comment: comment || undefined,
      });
      try {
        sessionStorage.setItem(TOKEN_KEY, token);
      } catch {
        // private mode: the token just isn't remembered
      }
      onClose();
      onDone();
    } catch (e) {
      setError(e instanceof ApiError ? `${e.status || ""} ${e.message}`.trim() : "The decision was not recorded.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      open={open}
      modalHeading="Decide on this remediation"
      modalLabel={`Remediation ${shortId(r.id)}`}
      primaryButtonText={busy ? "Recording…" : choice === "approve" ? "Approve this exact proposal" : "Reject"}
      secondaryButtonText="Cancel"
      danger={choice === "reject"}
      primaryButtonDisabled={busy || !token || !approver}
      onRequestClose={onClose}
      onRequestSubmit={submit}
      size="md"
    >
      <p className="ii-modal-intro">
        Your decision binds to this proposal hash and policy decision. If the proposal changes, it needs a new decision.
      </p>
      <dl className="ii-kv">
        <dt>Action</dt>
        <dd className="ii-mono">{r.action_id}</dd>
        <dt>Parameters</dt>
        <dd className="ii-mono ii-break">{JSON.stringify(r.parameters)}</dd>
        <dt>Environment</dt>
        <dd>{r.environment}</dd>
        <dt>Expected effect</dt>
        <dd>{r.expected_effect}</dd>
        <dt>Proposal hash</dt>
        <dd className="ii-mono ii-break">{r.proposal_hash}</dd>
      </dl>
      <div className="ii-form">
        <PasswordInput data-modal-primary-focus id="operator-token" name="operator-token" spellCheck={false} labelText="Operator API token" value={token} onChange={(e) => setToken(e.target.value)} autoComplete="off" />
        <TextInput id="approver" name="approver" autoComplete="off" spellCheck={false} labelText="Approver (must be on the approver roster)" value={approver} onChange={(e) => setApprover(e.target.value)} />
        <RadioButtonGroup legendText="Decision" name="decision" valueSelected={choice} onChange={(v) => setChoice(v as "approve" | "reject")}>
          <RadioButton id="d-approve" labelText="Approve" value="approve" />
          <RadioButton id="d-reject" labelText="Reject" value="reject" />
        </RadioButtonGroup>
        <TextArea id="comment" labelText="Comment (optional)" rows={2} value={comment} onChange={(e) => setComment(e.target.value)} />
        {error ? <InlineNotification kind="error" lowContrast hideCloseButton title="Not recorded" subtitle={error} /> : null}
      </div>
    </Modal>
  );
}

// --- timeline + impact ---------------------------------------------------------------

const KIND_LABEL: Record<string, string> = {
  alert: "Alert",
  state: "State",
  investigation: "Investigation",
  evidence: "Evidence",
  hypothesis: "Hypothesis",
  rca: "RCA",
  remediation: "Remediation",
  verification: "Verification",
};

function Timeline({ events, onEvidence }: { events: TimelineEvent[]; onEvidence: (id: string) => void }) {
  const [condensed, setCondensed] = useState(false);
  const shown = condensed ? events.filter((e) => e.kind !== "evidence" && e.kind !== "hypothesis") : events;
  return (
    <Panel
      title="Timeline"
      id="timeline"
      meta={
        <Toggle id="timeline-condensed" size="sm" labelText="Hide evidence steps" hideLabel toggled={condensed} onToggle={setCondensed} labelA="All steps" labelB="Key steps" />
      }
    >
      {shown.length === 0 ? <Empty>No events recorded yet.</Empty> : null}
      <ol className="ii-timeline">
        {shown.map((e, index) => (
          <li key={`${e.at}-${index}`} className={`ii-tl-${e.kind}`} data-kind={e.kind}>
            <time className="ii-mono" dateTime={e.at}>
              {clock(e.at)}
            </time>
            <span className="ii-tl-rule" aria-hidden="true" />
            <div>
              <span className="ii-tl-kind">{KIND_LABEL[e.kind] ?? e.kind}</span> {e.title}
              {e.detail ? <span className="ii-muted"> · {e.detail}</span> : null}
              {e.evidence_ids?.length ? (
                <div>
                  <EvidenceLinks ids={e.evidence_ids} onOpen={onEvidence} />
                </div>
              ) : null}
            </div>
          </li>
        ))}
      </ol>
    </Panel>
  );
}

function latestSample(detail: IncidentDetail): { baseline: Sample | null; observed: CheckResult[] | null; at: string | null } {
  const v = detail.verifications[detail.verifications.length - 1];
  if (!v) return { baseline: null, observed: null, at: null };
  const last = v.observations[v.observations.length - 1];
  return { baseline: v.verification.baseline, observed: last ? last.checks : null, at: last?.observed_at ?? null };
}

function Impact({ detail }: { detail: IncidentDetail }) {
  const rca = detail.investigations.find((i) => i.rca)?.rca;
  const services = new Set<string>([detail.incident.service]);
  detail.alerts.forEach((a) => a.service && services.add(a.service));
  rca?.report.affected_services.forEach((s) => services.add(s.service));
  const firing = detail.alerts.filter((a) => a.status === "firing").length;
  const { baseline } = latestSample(detail);
  const signals = baseline?.health?.signals ?? {};
  return (
    <Panel title="Impact" id="impact" meta={<span className="ii-muted">{`${detail.alerts.length} alert${detail.alerts.length === 1 ? "" : "s"} · ${firing} firing`}</span>}>
      <dl className="ii-kv">
        <dt>Affected services</dt>
        <dd>{[...services].sort().join(", ")}</dd>
        {rca ? (
          <>
            <dt>Impact (RCA)</dt>
            <dd>{rca.report.impact.text}</dd>
          </>
        ) : null}
      </dl>
      <ScrollRegion label={"Alerts"}>
      <Table size="sm" aria-label="Alerts" className="ii-inner-table">
        <TableHead>
          <TableRow>
            <TableHeader>Alert</TableHeader>
            <TableHeader>Status</TableHeader>
            <TableHeader>Fired</TableHeader>
          </TableRow>
        </TableHead>
        <TableBody>
          {detail.alerts.map((a) => (
            <TableRow key={a.id}>
              <TableCell>
                {a.alertname} <span className="ii-muted">· {a.service}</span>
              </TableCell>
              <TableCell>
                <StatusTag value={a.status} />
              </TableCell>
              <TableCell className="ii-mono">{clock(a.received_at)}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
      </ScrollRegion>
      {Object.keys(signals).length ? (
        <>
          <h3 className="ii-subhead">Health before remediation</h3>
          <dl className="ii-kv">
            {Object.entries(signals)
              .filter(([name]) => ["error_rate", "latency_p95", "request_rate", "cpu_usage", "memory_usage"].includes(name))
              .map(([name, value]) => (
                <div key={name} className="ii-kv-row">
                  <dt>{label(name)}</dt>
                  <dd className="ii-mono">{metricValue(name, value)}</dd>
                </div>
              ))}
          </dl>
        </>
      ) : null}
    </Panel>
  );
}

// --- investigation + RCA ---------------------------------------------------------------

function Rca({ detail, onEvidence }: { detail: IncidentDetail; onEvidence: (id: string) => void }) {
  const investigation = [...detail.investigations].reverse().find((i) => i.rca);
  if (!investigation?.rca) {
    const last = detail.investigations[detail.investigations.length - 1];
    return (
      <Panel title="Root cause" id="rca">
        <Empty>
          {last
            ? last.investigation.status === "INVESTIGATING" || last.investigation.status === "CREATED"
              ? "Investigation in progress."
              : `No accepted root cause (${label(last.investigation.status)}${last.investigation.inconclusive_reason ? `: ${last.investigation.inconclusive_reason}` : ""}).`
            : "Not investigated yet."}
        </Empty>
      </Panel>
    );
  }
  const report = investigation.rca.report;
  const root = report.root_cause_hypothesis;
  return (
    <Panel title="Root cause" id="rca" meta={<span className="ii-mono">confidence {report.confidence.toFixed(2)}</span>}>
      <p className="ii-rca-cause">
        <span className="ii-mono">{root.cause_category}</span> in <b>{root.component}</b>
      </p>
      <p>{report.root_cause.text}</p>
      <dl className="ii-kv">
        <dt>Summary</dt>
        <dd>{report.incident_summary.text}</dd>
        <dt>Supporting evidence</dt>
        <dd>
          <EvidenceLinks ids={report.supporting_evidence} onOpen={onEvidence} />
        </dd>
        <dt>Contradicting evidence</dt>
        <dd>
          {report.contradicting_evidence?.length ? (
            report.contradicting_evidence.map((c) => (
              <div key={c.evidence_id}>
                <EvidenceLinks ids={[c.evidence_id]} onOpen={onEvidence} /> {c.explanation}
              </div>
            ))
          ) : (
            <span className="ii-muted">none</span>
          )}
        </dd>
        <dt>Contributing factors</dt>
        <dd>
          {report.contributing_factors?.length ? (
            report.contributing_factors.map((f, i) => (
              <div key={i}>
                {f.text} <EvidenceLinks ids={f.evidence_ids} onOpen={onEvidence} />
              </div>
            ))
          ) : (
            <span className="ii-muted">none recorded</span>
          )}
        </dd>
        <dt>Unresolved questions</dt>
        <dd>{report.unresolved_questions?.length ? report.unresolved_questions.join(" · ") : <span className="ii-muted">none</span>}</dd>
        {report.recommended_next_diagnostic_action ? (
          <>
            <dt>Next diagnostic step</dt>
            <dd>{report.recommended_next_diagnostic_action}</dd>
          </>
        ) : null}
      </dl>
    </Panel>
  );
}

function Investigation({ investigations, onEvidence }: { investigations: InvestigationDetail[]; onEvidence: (id: string) => void }) {
  if (!investigations.length) {
    return (
      <Panel title="Investigation" id="investigation">
        <Empty>No investigation has started.</Empty>
      </Panel>
    );
  }
  return (
    <Panel title="Investigation" id="investigation" meta={<span className="ii-muted">{investigations.length} attempt{investigations.length === 1 ? "" : "s"}</span>}>
      {[...investigations].reverse().map((inv) => (
        <InvestigationAttempt key={inv.investigation.id} detail={inv} onEvidence={onEvidence} />
      ))}
    </Panel>
  );
}

function InvestigationAttempt({ detail, onEvidence }: { detail: InvestigationDetail; onEvidence: (id: string) => void }) {
  const i = detail.investigation;
  const hypotheses = useMemo(
    () => [...detail.hypotheses].sort((a, b) => (b.confidence ?? 0) - (a.confidence ?? 0)),
    [detail.hypotheses],
  );
  return (
    <div className="ii-attempt">
      <p className="ii-attempt-head">
        Attempt {i.attempt_number} <StatusTag value={i.status} />{" "}
        <span className="ii-muted">
          {i.model_provider}/{i.model_name} · {i.iteration_count} turns · {i.tool_call_count} tool calls · {i.evidence_count} evidence
        </span>
      </p>
      {hypotheses.length ? (
        <ScrollRegion label={`Hypotheses, attempt ${i.attempt_number}`}>
        <Table size="sm" aria-label={`Hypotheses, attempt ${i.attempt_number}`} className="ii-inner-table">
          <TableHead>
            <TableRow>
              <TableHeader>Hypothesis</TableHeader>
              <TableHeader>Status</TableHeader>
              <TableHeader>Confidence</TableHeader>
              <TableHeader>Supporting</TableHeader>
              <TableHeader>Contradicting</TableHeader>
            </TableRow>
          </TableHead>
          <TableBody>
            {hypotheses.map((h) => (
              <TableRow key={h.key} data-hypothesis={h.key}>
                <TableCell>
                  <span className="ii-mono">{h.cause_category}</span> · {h.component}
                  {h.missing_evidence.length ? <div className="ii-muted">missing: {h.missing_evidence.join("; ")}</div> : null}
                </TableCell>
                <TableCell>
                  <StatusTag value={h.status} />
                </TableCell>
                <TableCell className="ii-mono">{h.confidence === null ? "—" : h.confidence.toFixed(2)}</TableCell>
                <TableCell>
                  <EvidenceLinks ids={h.supporting_evidence_ids} onOpen={onEvidence} />
                </TableCell>
                <TableCell>
                  <EvidenceLinks ids={h.contradicting_evidence_ids} onOpen={onEvidence} />
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
        </ScrollRegion>
      ) : (
        <Empty>No hypotheses recorded.</Empty>
      )}
      {i.inconclusive_reason || i.escalation_reason ? (
        <p className="ii-muted">Ended: {i.inconclusive_reason ?? i.escalation_reason}</p>
      ) : null}
      <details className="ii-details">
        <summary>Tool activity ({detail.tool_calls.length})</summary>
        <ol className="ii-tools">
          {detail.tool_calls.map((c) => (
            <li key={c.sequence}>
              <span className="ii-mono">{clock(c.at)}</span> <span className="ii-mono">{c.tool}</span>{" "}
              {c.ok ? <span className="ii-muted">{c.summary}</span> : <StatusTag value="FAILED" prefix={c.error_code ?? ""} />}{" "}
              <EvidenceLinks ids={c.evidence_ids} onOpen={onEvidence} />
            </li>
          ))}
        </ol>
      </details>
    </div>
  );
}

// --- remediation + verification ------------------------------------------------------------

function Remediations({ remediations }: { remediations: RemediationDetail[] }) {
  if (!remediations.length) {
    return (
      <Panel title="Remediation" id="remediation">
        <Empty>No remediation proposed. Proposals come from the deterministic planner after an accepted RCA, or from an operator.</Empty>
      </Panel>
    );
  }
  return (
    <>
      {[...remediations].reverse().map((d) => (
        <RemediationPanel key={d.remediation.id} detail={d} />
      ))}
    </>
  );
}

function RemediationPanel({ detail }: { detail: RemediationDetail }) {
  const r = detail.remediation;
  const decision = detail.policy_decisions[0];
  const denied = decision?.rules.filter((rule) => rule.outcome === "deny") ?? [];
  const approvedBy = detail.timeline.find((t) => t.event === "approved");
  return (
    <Panel title="Remediation" id={`remediation-${shortId(r.id)}`} meta={<StatusTag value={r.status} />}>
      <dl className="ii-kv">
        <dt>Action</dt>
        <dd className="ii-mono">{r.action_id}</dd>
        <dt>Target</dt>
        <dd>
          {r.target_service} <span className="ii-muted">· {r.environment}</span>
        </dd>
        <dt>Parameters</dt>
        <dd className="ii-mono ii-break">{JSON.stringify(r.parameters)}</dd>
        <dt>Risk</dt>
        <dd>blast radius {r.blast_radius_tier ?? "—"}</dd>
        <dt>Policy</dt>
        <dd>
          <StatusTag value={r.policy_decision} /> <span className="ii-muted ii-mono">{decision?.policy_version}</span>
          {denied.map((rule) => (
            <div key={rule.rule_id} className="ii-denied">
              <span className="ii-mono">{rule.rule_id}</span>: {rule.detail}
            </div>
          ))}
        </dd>
        <dt>Approval</dt>
        <dd>
          <StatusTag value={r.approval_status ? r.approval_status.toUpperCase() : null} />
          {approvedBy ? <span className="ii-muted"> by {approvedBy.actor.replace("human:", "")} at {clock(approvedBy.occurred_at)}</span> : null}
        </dd>
        <dt>Execution</dt>
        <dd>
          <StatusTag value={r.execution_status} />{" "}
          <span className="ii-muted">
            {r.execution_attempts} attempt{r.execution_attempts === 1 ? "" : "s"}
          </span>
        </dd>
        {r.failure_reason ? (
          <>
            <dt>Reason</dt>
            <dd>{r.failure_reason}</dd>
          </>
        ) : null}
        <dt>Proposed by</dt>
        <dd>
          {r.source} · {r.proposed_by}
        </dd>
        <dt>Proposal hash</dt>
        <dd className="ii-mono ii-break">{r.proposal_hash}</dd>
      </dl>
    </Panel>
  );
}

function Verifications({ verifications, onEvidence }: { verifications: VerificationDetail[]; onEvidence: (id: string) => void }) {
  if (!verifications.length) {
    return (
      <Panel title="Verification" id="verification">
        <Empty>Nothing to verify yet. Verification starts when a remediation has executed.</Empty>
      </Panel>
    );
  }
  return (
    <>
      {[...verifications].reverse().map((v) => (
        <VerificationPanel key={v.verification.id} detail={v} onEvidence={onEvidence} />
      ))}
    </>
  );
}

function baselineFor(check: CheckResult, baseline: VerificationDetail["verification"]["baseline"]): string {
  if (!baseline) return "—";
  if (check.kind === "metric.max") {
    const metric = /^(\w+)/.exec(check.detail)?.[1] ?? "";
    return metricValue(metric, baseline.health?.signals?.[metric] ?? null);
  }
  if (check.kind === "health.status") return baseline.health?.status ?? "—";
  if (check.kind === "state.deployment_version") return baseline.deployment?.version ?? "—";
  if (check.kind === "state.replicas") return String(baseline.runtime?.replicas ?? "—");
  return "—";
}

function observedFor(check: CheckResult): string {
  if (check.kind === "metric.max") {
    const metric = /^(\w+)/.exec(check.detail)?.[1] ?? "";
    return metricValue(metric, typeof check.observed === "number" ? check.observed : null);
  }
  return check.observed === null || check.observed === undefined ? "—" : String(check.observed);
}

function expectedFor(check: CheckResult): string {
  if (check.kind === "metric.max") {
    const metric = /^(\w+)/.exec(check.detail)?.[1] ?? "";
    return `≤ ${metricValue(metric, typeof check.expected === "number" ? check.expected : null)}`;
  }
  if (check.kind === "alerts.no_new_firing") return "0 new";
  return check.expected === null || check.expected === undefined ? "—" : String(check.expected);
}

function VerificationPanel({ detail, onEvidence }: { detail: VerificationDetail; onEvidence: (id: string) => void }) {
  const v = detail.verification;
  const last = detail.observations[detail.observations.length - 1];
  const spec = v.spec;
  return (
    <Panel title="Verification" id="verification" meta={<StatusTag value={v.status} />}>
      <dl className="ii-kv">
        <dt>Result</dt>
        <dd>
          {v.consecutive_successes}/{spec.required_consecutive} consecutive passing observations · {v.observation_count} observed
          {v.next_action ? <span className="ii-muted"> · next: {v.next_action}</span> : null}
        </dd>
        {v.failure_reason ? (
          <>
            <dt>Reason</dt>
            <dd>{v.failure_reason}</dd>
          </>
        ) : null}
        <dt>Window</dt>
        <dd className="ii-mono">
          grace {duration(spec.grace_seconds)} · poll every {duration(spec.poll_interval_seconds)} · timeout {duration(spec.timeout_seconds)}
        </dd>
        <dt>Policy</dt>
        <dd className="ii-mono">{v.policy_version}</dd>
        <dt>Baseline evidence</dt>
        <dd>
          <EvidenceLinks ids={v.baseline?.evidence_ids ?? []} onOpen={onEvidence} />
        </dd>
      </dl>
      {last ? (
        <ScrollRegion label={"Checks: baseline and latest observation"}>
        <Table size="sm" aria-label="Checks: baseline and latest observation" className="ii-inner-table ii-checks">
          <TableHead>
            <TableRow>
              <TableHeader>Check</TableHeader>
              <TableHeader>Before</TableHeader>
              <TableHeader>Latest</TableHeader>
              <TableHeader>Expected</TableHeader>
              <TableHeader>OK</TableHeader>
            </TableRow>
          </TableHead>
          <TableBody>
            {last.checks.map((c) => (
              <TableRow key={c.kind + c.detail} data-check={c.kind}>
                <TableCell className="ii-mono">{c.kind === "metric.max" ? /^(\w+)/.exec(c.detail)?.[1] : c.kind}</TableCell>
                <TableCell className="ii-mono">{baselineFor(c, v.baseline)}</TableCell>
                <TableCell className="ii-mono">{observedFor(c)}</TableCell>
                <TableCell className="ii-mono">{expectedFor(c)}</TableCell>
                <TableCell>
                  <StatusTag value={c.ok === null ? "PENDING" : c.ok ? "PASSED" : "FAILED"} />
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
        </ScrollRegion>
      ) : (
        <Empty>{v.status === "PENDING" || v.status === "RUNNING" ? "Waiting for the first observation (grace period)." : "No observations were recorded."}</Empty>
      )}
      {detail.observations.length ? (
        <details className="ii-details">
          <summary>Observations ({detail.observations.length})</summary>
          <ol className="ii-tools">
            {detail.observations.map((o) => (
              <li key={o.sequence}>
                <span className="ii-mono">
                  #{o.sequence} {clock(o.observed_at)}
                </span>{" "}
                <StatusTag value={o.passed ? "PASSED" : o.conclusive ? "FAILED" : "PENDING"} />{" "}
                <span className="ii-muted">{o.checks.filter((c) => c.ok === false).map((c) => c.detail).join("; ") || o.errors.join("; ")}</span>{" "}
                <EvidenceLinks ids={o.evidence_ids} onOpen={onEvidence} />
              </li>
            ))}
          </ol>
        </details>
      ) : null}
    </Panel>
  );
}
