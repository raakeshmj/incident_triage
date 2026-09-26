import type { Page, Route } from "@playwright/test";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const load = (name: string) => JSON.parse(readFileSync(fileURLToPath(new URL(`../tests/fixtures/${name}.json`, import.meta.url)), "utf8"));

export const fx = {
  incidents: load("api_v1_incidents"),
  overview: load("api_v1_overview"),
  metrics: load("api_v1_metrics"),
  evidence: load("evidence"),
  resolved: load("detail_resolved"),
  failed: load("detail_verification_failed"),
  awaiting: load("detail_awaiting_approval"),
  triaging: load("detail_triaging"),
  escalated: load("detail_escalated"),
  rcaReady: load("detail_rca_ready"),
};

const details = [fx.resolved, fx.failed, fx.awaiting, fx.triaging, fx.escalated, fx.rcaReady];

export type Override = (route: Route, url: URL) => Promise<boolean> | boolean;

export async function mockApi(page: Page, override?: Override) {
  await page.route("**/api/v1/**", async (route) => {
    const url = new URL(route.request().url());
    if (override && (await override(route, url))) return;
    const p = url.pathname;
    const body = (b: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(b) });
    if (p === "/api/v1/incidents") {
      const status = url.searchParams.getAll("status");
      const items = status.length ? fx.incidents.items.filter((i: { status: string }) => status.includes(i.status)) : fx.incidents.items;
      return body({ ...fx.incidents, total: items.length, items });
    }
    if (p === "/api/v1/overview") return body(fx.overview);
    if (p === "/api/v1/metrics") return body(fx.metrics);
    if (p.startsWith("/api/v1/evidence/")) return body(fx.evidence);
    const m = p.match(/^\/api\/v1\/incidents\/([^/]+)\/detail$/);
    const found = m && details.find((d) => d.incident.id === m[1]);
    return found ? body(found) : body({ detail: "incident not found" }, 404);
  });
}
