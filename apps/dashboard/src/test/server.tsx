// A fake API for component tests: routes fetch() to fixtures captured from
// the real API (tests/fixtures), so the tests exercise the real contract.
import { render } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { vi } from "vitest";

import incidents from "../../tests/fixtures/api_v1_incidents.json";
import metrics from "../../tests/fixtures/api_v1_metrics.json";
import overview from "../../tests/fixtures/api_v1_overview.json";
import awaiting from "../../tests/fixtures/detail_awaiting_approval.json";
import escalated from "../../tests/fixtures/detail_escalated.json";
import rcaReady from "../../tests/fixtures/detail_rca_ready.json";
import resolved from "../../tests/fixtures/detail_resolved.json";
import triaging from "../../tests/fixtures/detail_triaging.json";
import verificationFailed from "../../tests/fixtures/detail_verification_failed.json";
import evidence from "../../tests/fixtures/evidence.json";
import { App } from "../App";

export const fixtures = { incidents, metrics, overview, evidence, details: { awaiting, escalated, rcaReady, resolved, triaging, verificationFailed } };

type Handler = (url: URL, init?: RequestInit) => Response | Promise<Response>;

export const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

export function defaultHandler(url: URL): Response {
  const p = url.pathname;
  if (p === "/api/v1/incidents") return json(incidents);
  if (p === "/api/v1/overview") return json(overview);
  if (p === "/api/v1/metrics") return json(metrics);
  if (p.startsWith("/api/v1/evidence/")) return json(evidence);
  const m = p.match(/^\/api\/v1\/incidents\/([^/]+)\/detail$/);
  if (m) {
    const found = Object.values(fixtures.details).find((d) => d.incident.id === m[1]);
    return found ? json(found) : json({ detail: "incident not found" }, 404);
  }
  return json({ detail: "not found" }, 404);
}

export function mockApi(handler: Handler = defaultHandler) {
  const calls: { url: URL; init?: RequestInit }[] = [];
  const spy = vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const url = new URL(String(input), "http://localhost");
    calls.push({ url, init });
    return handler(url, init);
  });
  return { spy, calls };
}

export function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <App />
    </MemoryRouter>,
  );
}
