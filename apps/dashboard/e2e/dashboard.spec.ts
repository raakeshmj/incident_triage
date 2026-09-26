import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";

import { fx, mockApi } from "./mock";

test.describe("mocked API", () => {
  test.beforeEach(async ({ page }) => {
    await mockApi(page);
  });

  test("incident list loads and links to details", async ({ page }) => {
    await page.goto("/");
    await expect(page.getByRole("heading", { level: 1, name: "Incidents" })).toBeVisible();
    await expect(page.getByText("10 incidents · 4 active in view")).toBeVisible();
    await page.getByRole("link", { name: new RegExp(fx.failed.incident.id.slice(0, 8)) }).first().click();
    await expect(page).toHaveURL(new RegExp(`/incidents/${fx.failed.incident.id}`));
    await expect(page.getByRole("heading", { level: 1 })).toContainText("checkout-service");
  });

  test("filters narrow the list and survive reload", async ({ page }) => {
    await page.goto("/");
    await page.getByLabel("Status").selectOption("RESOLVED");
    await expect(page).toHaveURL(/status=RESOLVED/);
    await expect(page.getByText("4 incidents")).toBeVisible();
    await page.reload();
    await expect(page.getByLabel("Status")).toHaveValue("RESOLVED");
    await expect(page.getByText("4 incidents")).toBeVisible();
  });

  test("detail shows timeline, remediation and a failed verification", async ({ page }) => {
    await page.goto(`/incidents/${fx.failed.incident.id}`);
    const timeline = page.getByRole("region", { name: "Timeline" });
    await expect(timeline.getByRole("listitem")).toHaveCount(fx.failed.timeline.length);
    await expect(page.getByRole("region", { name: "Remediation" }).getByText("rollback_deployment")).toBeVisible();
    const verification = page.getByRole("region", { name: "Verification" });
    await expect(verification.getByText(/next: reinvestigate/)).toBeVisible();
    await expect(verification.getByText("health.status")).toBeVisible();
  });

  test("evidence links open the evidence record", async ({ page }) => {
    await page.goto(`/incidents/${fx.resolved.incident.id}`);
    await page.getByRole("region", { name: "Timeline" }).getByRole("button", { name: /^Open evidence / }).first().click();
    const dialog = page.getByRole("dialog", { name: /Evidence/ });
    await expect(dialog.getByText(fx.evidence.summary)).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(dialog).toBeHidden();
  });

  test("a resolved incident shows a passed verification and no approval action", async ({ page }) => {
    await page.goto(`/incidents/${fx.resolved.incident.id}`);
    await expect(page.getByRole("region", { name: "Verification" }).getByText("Passed").first()).toBeVisible();
    await expect(page.getByRole("button", { name: "Review and decide" })).toHaveCount(0);
  });

  test("approval flow: state change renders after the decision", async ({ page }) => {
    let approved = false;
    await page.unroute("**/api/v1/**");
    await mockApi(page, async (route, url) => {
      if (route.request().method() === "POST") {
        const sent = route.request().postDataJSON();
        expect(sent.proposal_hash).toBe(fx.awaiting.remediations[0].remediation.proposal_hash);
        expect(route.request().headers()["authorization"]).toBe("Bearer op-token");
        approved = true;
        await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(fx.awaiting.remediations[0].remediation) });
        return true;
      }
      if (approved && url.pathname.endsWith(`${fx.awaiting.incident.id}/detail`)) {
        const next = structuredClone(fx.awaiting);
        next.incident.status = "REMEDIATION_IN_PROGRESS";
        next.remediations[0].remediation.status = "APPROVED";
        await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(next) });
        return true;
      }
      return false;
    });
    await page.goto(`/incidents/${fx.awaiting.incident.id}`);
    await page.getByRole("button", { name: "Review and decide" }).click();
    const dialog = page.getByRole("dialog", { name: /^Remediation / });
    await dialog.getByLabel("Operator API token").fill("op-token");
    await dialog.getByLabel(/Approver/).fill("alice");
    await dialog.getByRole("button", { name: "Approve this exact proposal" }).click();
    await expect(page.getByRole("button", { name: "Review and decide" })).toHaveCount(0);
    await expect(page.locator("header").getByText("Remediation in progress").first()).toBeVisible();
  });

  test("operations overview shows DLQ, workers and metrics", async ({ page }) => {
    await page.goto("/operations");
    await expect(page.getByText("Dead letters")).toBeVisible();
    await expect(page.getByRole("region", { name: "Workers" })).toBeVisible();
    await expect(page.getByText("Verification latency")).toBeVisible();
  });

  test("no horizontal overflow on any page", async ({ page }) => {
    for (const path of ["/", `/incidents/${fx.failed.incident.id}`, "/operations"]) {
      await page.goto(path);
      await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
      expect(overflow, path).toBeLessThanOrEqual(0);
    }
  });

  test("keyboard: skip link and focusable incident links", async ({ page, isMobile }) => {
    test.skip(isMobile, "keyboard navigation is a desktop concern");
    await page.goto("/");
    await expect(page.getByRole("table", { name: "Incidents" })).toBeVisible();
    await page.keyboard.press("Tab");
    await expect(page.getByRole("link", { name: "Skip to main content" })).toBeFocused();
    await page.keyboard.press("Enter");
    for (let i = 0; i < 12; i++) {
      await page.keyboard.press("Tab");
      const focused = await page.evaluate(() => document.activeElement?.getAttribute("href") ?? "");
      if (focused.startsWith("/incidents/")) return;
    }
    throw new Error("no incident link reachable by keyboard");
  });

  test("accessibility: no serious axe violations", async ({ page }) => {
    for (const path of ["/", `/incidents/${fx.failed.incident.id}`, `/incidents/${fx.awaiting.incident.id}`, "/operations"]) {
      await page.goto(path);
      await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
      const result = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa"]).analyze();
      const serious = result.violations.filter((v) => v.impact === "serious" || v.impact === "critical");
      expect(serious.map((v) => `${path}: ${v.id} ${v.nodes.map((n) => n.target.join(" ")).join(", ")}`)).toEqual([]);
    }
  });
});

test.describe("states", () => {
  test("loading, error and retry", async ({ page }) => {
    let release!: () => void;
    const gate = new Promise<void>((r) => (release = r));
    let fail = true;
    await mockApi(page, async (route, url) => {
      if (url.pathname !== "/api/v1/incidents") return false;
      await gate;
      if (fail) {
        await route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: "database unavailable" }) });
        return true;
      }
      return false;
    });
    await page.goto("/");
    await expect(page.locator(".cds--skeleton").first()).toBeVisible();
    release();
    await expect(page.getByRole("alert")).toContainText("database unavailable");
    fail = false;
    await page.getByRole("button", { name: "Retry" }).click();
    await expect(page.getByText("10 incidents")).toBeVisible();
  });

  test("empty state", async ({ page }) => {
    await mockApi(page, async (route, url) => {
      if (url.pathname !== "/api/v1/incidents") return false;
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ total: 0, items: [], services: [] }) });
      return true;
    });
    await page.goto("/");
    await expect(page.getByText(/No incidents yet/)).toBeVisible();
  });

  test("API unreachable", async ({ page }) => {
    await page.route("**/api/v1/**", (route) => route.abort());
    await page.goto("/operations");
    await expect(page.getByRole("alert").first()).toContainText("unreachable");
  });
});
