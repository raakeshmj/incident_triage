import {
  DataTableSkeleton,
  Select,
  SelectItem,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableHeader,
  TableRow,
} from "@carbon/react";
import { Link, useSearchParams } from "react-router-dom";

import { api } from "../api/client";
import { clock, dateTime, duration, shortId } from "../api/format";
import type { IncidentFilters, IncidentSummary } from "../api/types";
import { Empty, LoadError, StatusTag, useLoad } from "../components/common";
import { useMedia } from "../components/useMedia";

const STATUSES = [
  "TRIAGING",
  "INVESTIGATING",
  "RCA_READY",
  "AWAITING_APPROVAL",
  "REMEDIATION_IN_PROGRESS",
  "VERIFYING",
  "VERIFICATION_FAILED",
  "RESOLVED",
  "ESCALATED",
  "CANCELLED",
];
const RANGES = [
  { value: "all", text: "Any time" },
  { value: "1h", text: "Last hour" },
  { value: "24h", text: "Last 24 hours" },
  { value: "7d", text: "Last 7 days" },
];
const ACTIVE = new Set(["TRIAGING", "INVESTIGATING", "RCA_READY", "AWAITING_APPROVAL", "REMEDIATION_IN_PROGRESS", "VERIFYING", "VERIFICATION_FAILED"]);

export function IncidentListPage() {
  const [params, setParams] = useSearchParams();
  const filters: IncidentFilters = {
    status: params.get("status") ?? undefined,
    severity: params.get("severity") ?? undefined,
    service: params.get("service") ?? undefined,
    range: params.get("range") ?? undefined,
  };
  const key = params.toString();
  const { data, error, loading, refresh } = useLoad(() => api.incidents(filters), [key], 15000);
  const narrow = useMedia("(max-width: 671px)");
  const medium = useMedia("(max-width: 1055px)");

  const set = (name: string, value: string) => {
    const next = new URLSearchParams(params);
    if (value && value !== "all") next.set(name, value);
    else next.delete(name);
    setParams(next, { replace: true });
  };
  const active = data ? data.items.filter((i) => ACTIVE.has(i.status)).length : 0;

  return (
    <div className="ii-page">
      <h1 className="ii-title">Incidents</h1>
      <p className="ii-muted ii-lede">
        {data ? `${data.total} incident${data.total === 1 ? "" : "s"} · ${active} active in view` : " "}
      </p>
      <div className="ii-filters" role="search" aria-label="Filter incidents">
        <Select id="f-status" labelText="Status" size="md" value={filters.status ?? "all"} onChange={(e) => set("status", e.target.value)}>
          <SelectItem value="all" text="All statuses" />
          {STATUSES.map((s) => (
            <SelectItem key={s} value={s} text={s.replaceAll("_", " ").toLowerCase()} />
          ))}
        </Select>
        <Select id="f-severity" labelText="Severity" size="md" value={filters.severity ?? "all"} onChange={(e) => set("severity", e.target.value)}>
          <SelectItem value="all" text="All severities" />
          {["critical", "warning", "info"].map((s) => (
            <SelectItem key={s} value={s} text={s} />
          ))}
        </Select>
        <Select id="f-service" labelText="Service" size="md" value={filters.service ?? "all"} onChange={(e) => set("service", e.target.value)}>
          <SelectItem value="all" text="All services" />
          {(data?.services ?? (filters.service ? [filters.service] : [])).map((s) => (
            <SelectItem key={s} value={s} text={s} />
          ))}
        </Select>
        <Select id="f-range" labelText="Opened" size="md" value={filters.range ?? "all"} onChange={(e) => set("range", e.target.value)}>
          {RANGES.map((r) => (
            <SelectItem key={r.value} value={r.value} text={r.text} />
          ))}
        </Select>
      </div>

      {error ? <LoadError error={error} onRetry={refresh} /> : null}
      {loading && !data ? <DataTableSkeleton columnCount={narrow ? 3 : 8} rowCount={6} showHeader={false} showToolbar={false} /> : null}
      {data && data.items.length === 0 ? (
        <Empty>{key ? "No incidents match these filters." : "No incidents yet. Alerts that correlate into incidents appear here."}</Empty>
      ) : null}
      {data && data.items.length > 0 && narrow ? <StackedList items={data.items} /> : null}
      {data && data.items.length > 0 && !narrow ? <IncidentTable items={data.items} compact={medium} /> : null}
    </div>
  );
}

function IncidentTable({ items, compact }: { items: IncidentSummary[]; compact: boolean }) {
  return (
    <TableContainer className="ii-table">
      <Table size="md" aria-label="Incidents">
        <TableHead>
          <TableRow>
            <TableHeader>Incident</TableHeader>
            <TableHeader>Severity</TableHeader>
            <TableHeader>Service</TableHeader>
            {!compact ? <TableHeader>Environment</TableHeader> : null}
            <TableHeader>Status</TableHeader>
            <TableHeader>Opened</TableHeader>
            <TableHeader>Duration</TableHeader>
            {!compact ? <TableHeader>Services</TableHeader> : null}
            {!compact ? <TableHeader>Investigation</TableHeader> : null}
            <TableHeader>Remediation</TableHeader>
          </TableRow>
        </TableHead>
        <TableBody>
          {items.map((i) => (
            <TableRow key={i.id} data-incident={i.id}>
              <TableCell>
                <Link className="cds--link ii-mono" to={`/incidents/${i.id}`} aria-label={`Incident ${i.id} on ${i.service}`}>
                  {shortId(i.id)}
                </Link>
              </TableCell>
              <TableCell>
                <StatusTag value={i.severity} />
              </TableCell>
              <TableCell>{i.service}</TableCell>
              {!compact ? <TableCell>{i.environment}</TableCell> : null}
              <TableCell>
                <StatusTag value={i.status} />
              </TableCell>
              <TableCell className="ii-mono" title={dateTime(i.created_at)}>
                {clock(i.created_at)}
              </TableCell>
              <TableCell className="ii-mono">{duration(i.duration_seconds)}</TableCell>
              {!compact ? <TableCell>{i.affected_service_count}</TableCell> : null}
              {!compact ? (
                <TableCell>
                  <StatusTag value={i.investigation_status} />
                </TableCell>
              ) : null}
              <TableCell>
                <RemediationState item={i} />
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </TableContainer>
  );
}

function RemediationState({ item }: { item: IncidentSummary }) {
  if (!item.remediation_status) return <span className="ii-muted">—</span>;
  return (
    <span className="ii-stack">
      <StatusTag value={item.remediation_status} />
      {item.verification_status ? <StatusTag value={item.verification_status} prefix="verification" /> : null}
    </span>
  );
}

function StackedList({ items }: { items: IncidentSummary[] }) {
  return (
    <ul className="ii-stacked" aria-label="Incidents">
      {items.map((i) => (
        <li key={i.id} data-incident={i.id}>
          <Link to={`/incidents/${i.id}`} className="ii-stacked-link" aria-label={`Incident ${i.id} on ${i.service}`}>
            <span className="ii-stacked-top">
              <span className="ii-mono">{shortId(i.id)}</span>
              <StatusTag value={i.status} />
            </span>
            <span>
              {i.service} <span className="ii-muted">· {i.environment}</span>
            </span>
            <span className="ii-muted ii-mono">
              {clock(i.created_at)} · {duration(i.duration_seconds)} · {i.severity}
            </span>
          </Link>
        </li>
      ))}
    </ul>
  );
}
