import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@carbon/react";
import { Link } from "react-router-dom";

import { api } from "../api/client";
import { clock, duration, label, percent, shortId } from "../api/format";
import type { Metrics } from "../api/types";
import { Empty, Loading, LoadError, Panel, ScrollRegion, StatusTag, useLoad } from "../components/common";

const DURATIONS: { key: string; name: string }[] = [
  { key: "time_to_incident_creation", name: "Time to incident creation" },
  { key: "time_to_investigation_start", name: "Time to investigation start" },
  { key: "investigation_duration", name: "Investigation duration" },
  { key: "time_to_rca", name: "Time to RCA" },
  { key: "approval_latency", name: "Approval latency" },
  { key: "remediation_execution_latency", name: "Remediation execution latency" },
  { key: "verification_latency", name: "Verification latency" },
  { key: "incident_duration", name: "Incident duration" },
];
const RATES: { key: string; name: string }[] = [
  { key: "successful_remediation_rate", name: "Successful remediation (verification passed)" },
  { key: "verification_failure_rate", name: "Verification failed or timed out" },
  { key: "escalation_rate", name: "Escalated (of finished incidents)" },
];

export function OperationsPage() {
  const overview = useLoad(() => api.overview(), [], 10000);
  const metrics = useLoad(() => api.metrics(), [], 30000);
  const o = overview.data;
  return (
    <div className="ii-page">
      <h1 className="ii-title">Operations</h1>
      {overview.error ? <LoadError error={overview.error} onRetry={overview.refresh} /> : null}
      {overview.loading && !o ? <Loading lines={6} /> : null}
      {o ? (
        <>
          <dl className="ii-tiles" aria-label="Current activity">
            {[
              ["Active incidents", o.active_incidents],
              ["Investigations running", o.investigations_running],
              ["Awaiting approval", o.awaiting_approval],
              ["Remediations executing", o.remediations_executing],
              ["Verifications running", o.verifications_running],
              ["Dead letters", o.dead_letter_count ?? "—"],
              ["Failed outbox events", o.outbox_failed_events],
            ].map(([name, value]) => (
              <div key={name}>
                <dt>{name}</dt>
                <dd>{value}</dd>
              </div>
            ))}
          </dl>
          <div className="ii-two">
            <Panel title="Workers" id="workers" meta={o.redis_available ? <span className="ii-muted">heartbeats (30s TTL)</span> : <span className="ii-error-text">Redis unreachable</span>}>
              {o.workers.length === 0 ? (
                <Empty>No worker heartbeat in the last 30 seconds.</Empty>
              ) : (
                <ScrollRegion label="Workers">
                <Table size="sm" aria-label="Workers" className="ii-inner-table">
                  <TableHead>
                    <TableRow>
                      <TableHeader>Worker</TableHeader>
                      <TableHeader>Instance</TableHeader>
                      <TableHeader>Last beat</TableHeader>
                    </TableRow>
                  </TableHead>
                  <TableBody>
                    {o.workers.map((w) => (
                      <TableRow key={w.key}>
                        <TableCell>{w.role}</TableCell>
                        <TableCell className="ii-mono ii-break">{w.key.split(":").slice(2).join(":")}</TableCell>
                        <TableCell className="ii-mono">{duration(w.age_seconds)} ago</TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
                </ScrollRegion>
              )}
            </Panel>
            <Panel title="Recent escalations" id="escalations">
              {o.recent_escalations.length === 0 ? (
                <Empty>No escalations.</Empty>
              ) : (
                <ScrollRegion label="Recent escalations">
                <Table size="sm" aria-label="Recent escalations" className="ii-inner-table">
                  <TableHead>
                    <TableRow>
                      <TableHeader>Incident</TableHeader>
                      <TableHeader>Reason</TableHeader>
                      <TableHeader>At</TableHeader>
                    </TableRow>
                  </TableHead>
                  <TableBody>
                    {o.recent_escalations.map((e) => (
                      <TableRow key={e.incident_id + e.at}>
                        <TableCell>
                          <Link className="cds--link ii-mono" to={`/incidents/${e.incident_id}`}>
                            {shortId(e.incident_id)}
                          </Link>
                        </TableCell>
                        <TableCell>{label(e.reason)}</TableCell>
                        <TableCell className="ii-mono">{clock(e.at)}</TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
                </ScrollRegion>
              )}
            </Panel>
          </div>
          <Panel title="Incidents by status" id="by-status">
            <ul className="ii-status-counts">
              {Object.entries(o.incidents_by_status)
                .sort()
                .map(([status, count]) => (
                  <li key={status}>
                    <Link to={`/?status=${status}`}>
                      <StatusTag value={status} /> <span className="ii-mono">{count}</span>
                    </Link>
                  </li>
                ))}
            </ul>
          </Panel>
        </>
      ) : null}
      <MetricsPanel metrics={metrics.data} error={metrics.error} />
    </div>
  );
}

function MetricsPanel({ metrics, error }: { metrics: Metrics | null; error: unknown }) {
  return (
    <Panel title="Lifecycle metrics" id="metrics" meta={<span className="ii-muted">computed from recorded timestamps only</span>}>
      {error ? <LoadError error={error} /> : null}
      {!metrics && !error ? <Loading /> : null}
      {metrics ? (
        <>
          <ScrollRegion label="Lifecycle durations">
          <Table size="sm" aria-label="Lifecycle durations" className="ii-inner-table">
            <TableHead>
              <TableRow>
                <TableHeader>Duration</TableHeader>
                <TableHeader>n</TableHeader>
                <TableHeader>p50</TableHeader>
                <TableHeader>p90</TableHeader>
                <TableHeader>Max</TableHeader>
              </TableRow>
            </TableHead>
            <TableBody>
              {DURATIONS.map(({ key, name }) => {
                const m = metrics[key];
                return (
                  <TableRow key={key} data-metric={key}>
                    <TableCell>{name}</TableCell>
                    <TableCell className="ii-mono">{m?.count ?? 0}</TableCell>
                    <TableCell className="ii-mono">{m?.count ? duration(m.p50) : "no data"}</TableCell>
                    <TableCell className="ii-mono">{m?.count ? duration(m.p90) : "—"}</TableCell>
                    <TableCell className="ii-mono">{m?.count ? duration(m.max) : "—"}</TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
          </ScrollRegion>
          <ScrollRegion label="Lifecycle rates">
          <Table size="sm" aria-label="Lifecycle rates" className="ii-inner-table">
            <TableHead>
              <TableRow>
                <TableHeader>Rate</TableHeader>
                <TableHeader>Of</TableHeader>
                <TableHeader>Value</TableHeader>
              </TableRow>
            </TableHead>
            <TableBody>
              {RATES.map(({ key, name }) => {
                const m = metrics[key];
                return (
                  <TableRow key={key} data-metric={key}>
                    <TableCell>{name}</TableCell>
                    <TableCell className="ii-mono">{m?.count ?? 0}</TableCell>
                    <TableCell className="ii-mono">{m?.count ? `${percent(m.rate, 0)} (${m.numerator}/${m.count})` : "no data"}</TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
          </ScrollRegion>
        </>
      ) : null}
    </Panel>
  );
}
