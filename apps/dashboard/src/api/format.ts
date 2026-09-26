// Presentation helpers only: how a value is shown, never what it means.

export type Tone = "red" | "yellow" | "green" | "blue" | "gray";

const TONES: Record<string, Tone> = {
  // incident
  TRIAGING: "blue",
  INVESTIGATING: "blue",
  RCA_READY: "blue",
  AWAITING_APPROVAL: "yellow",
  REMEDIATION_IN_PROGRESS: "blue",
  VERIFYING: "blue",
  VERIFICATION_FAILED: "red",
  RESOLVED: "green",
  ESCALATED: "red",
  CANCELLED: "gray",
  CLOSED: "gray",
  SUPPRESSED: "gray",
  // investigation / remediation / verification / hypotheses
  COMPLETED: "green",
  FAILED: "red",
  CREATED: "gray",
  PROPOSED: "gray",
  POLICY_REJECTED: "red",
  APPROVED: "blue",
  EXECUTING: "blue",
  EXECUTED: "green",
  PENDING: "gray",
  RUNNING: "blue",
  PASSED: "green",
  TIMED_OUT: "red",
  SELECTED: "green",
  SUPPORTED: "green",
  ACTIVE: "blue",
  WEAKENED: "gray",
  REJECTED: "gray",
  REQUIRE_APPROVAL: "yellow",
  DENY: "red",
  ALLOW: "green",
  critical: "red",
  warning: "yellow",
  info: "blue",
  firing: "red",
  resolved: "green",
};

export function tone(value: string | null | undefined): Tone {
  return (value && TONES[value]) || "gray";
}

const ACRONYMS: Record<string, string> = { rca: "RCA", dlq: "DLQ", id: "ID", cpu: "CPU" };

export function label(value: string | null | undefined): string {
  if (!value) return "—";
  const words = value.replaceAll("_", " ").toLowerCase().split(" ");
  return words
    .map((w, i) => ACRONYMS[w] ?? (i === 0 ? w.charAt(0).toUpperCase() + w.slice(1) : w))
    .join(" ");
}

export function shortId(id: string): string {
  return id.slice(0, 8);
}

export function duration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || Number.isNaN(seconds)) return "—";
  if (seconds < 1) return `${Math.round(seconds * 1000)}ms`;
  const s = Math.round(seconds);
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  const r = s % 60;
  if (d) return `${d}d ${h}h`;
  if (h) return `${h}h ${m}m`;
  if (m) return `${m}m ${String(r).padStart(2, "0")}s`;
  return `${r}s`;
}

// Incident times are shown in UTC (one clock for every responder), formatted
// with Intl rather than by hand.
const UTC_TIME = new Intl.DateTimeFormat("en-GB", { timeZone: "UTC", hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" });
const UTC_DATE = new Intl.DateTimeFormat("en-CA", { timeZone: "UTC", year: "numeric", month: "2-digit", day: "2-digit" });

export function clock(iso: string | null | undefined): string {
  if (!iso) return "—";
  return UTC_TIME.format(new Date(iso));
}

export function dateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return `${UTC_DATE.format(d)} ${UTC_TIME.format(d)}Z`;
}

export function percent(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined) return "—";
  return `${(value * 100).toFixed(digits)}%`;
}

export function metricValue(name: string, value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  if (name.includes("error_rate")) return percent(value, 2);
  if (name.includes("latency")) return value < 1 ? `${Math.round(value * 1000)}ms` : `${value.toFixed(2)}s`;
  if (name.includes("memory")) return `${(value / 1048576).toFixed(0)}MB`;
  if (name.includes("cpu")) return percent(value, 0);
  return String(Math.round(value * 1000) / 1000);
}

// The lifecycle an incident moves through; used to draw progress, not to decide it.
export const LIFECYCLE = [
  { key: "TRIAGING", label: "Triage" },
  { key: "INVESTIGATING", label: "Investigate" },
  { key: "RCA_READY", label: "RCA" },
  { key: "AWAITING_APPROVAL", label: "Approval" },
  { key: "REMEDIATION_IN_PROGRESS", label: "Execute" },
  { key: "VERIFYING", label: "Verify" },
  { key: "RESOLVED", label: "Resolved" },
] as const;
