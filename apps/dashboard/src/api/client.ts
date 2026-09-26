// The dashboard's only way to the backend: the Incident Intelligence API.
// No database access, no domain logic -- fetch, check, return typed data.

import type {
  EvidenceDetail,
  IncidentDetail,
  IncidentFilters,
  IncidentList,
  Metrics,
  Overview,
  Remediation,
} from "./types";

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

const RANGES: Record<string, number> = { "1h": 3600e3, "24h": 86400e3, "7d": 7 * 86400e3 };

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, { ...init, headers: { Accept: "application/json", ...(init?.headers ?? {}) } });
  } catch {
    throw new ApiError(0, "The API is unreachable.");
  }
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = await response.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail ?? body);
    } catch {
      // keep the status text
    }
    throw new ApiError(response.status, detail || `HTTP ${response.status}`);
  }
  return (await response.json()) as T;
}

export function incidentQuery(filters: IncidentFilters, now: Date = new Date()): string {
  const params = new URLSearchParams();
  if (filters.status) params.append("status", filters.status);
  if (filters.severity) params.append("severity", filters.severity);
  if (filters.service) params.set("service", filters.service);
  const span = filters.range ? RANGES[filters.range] : undefined;
  if (span) params.set("since", new Date(now.getTime() - span).toISOString());
  params.set("limit", "200");
  return params.toString();
}

export const api = {
  incidents: (filters: IncidentFilters) => request<IncidentList>(`/api/v1/incidents?${incidentQuery(filters)}`),
  incident: (id: string) => request<IncidentDetail>(`/api/v1/incidents/${encodeURIComponent(id)}/detail`),
  evidence: (id: string) => request<EvidenceDetail>(`/api/v1/evidence/${encodeURIComponent(id)}`),
  overview: () => request<Overview>("/api/v1/overview"),
  metrics: () => request<Metrics>("/api/v1/metrics"),
  decide: (
    remediationId: string,
    token: string,
    body: { approver: string; decision: "approve" | "reject"; proposal_hash: string; policy_decision_id: string; comment?: string },
  ) =>
    request<Remediation>(`/api/v1/remediations/${encodeURIComponent(remediationId)}/approval`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
      body: JSON.stringify(body),
    }),
};
