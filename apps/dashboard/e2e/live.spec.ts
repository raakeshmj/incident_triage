import { expect, test } from "@playwright/test";

// Against the real API (vite preview proxies /api). Skipped unless
// DASHBOARD_LIVE=1: needs the API up with incidents (scripts/seed_demo.py).
test.skip(!process.env.DASHBOARD_LIVE, "set DASHBOARD_LIVE=1 with the API running");

test("real API: list -> detail -> evidence", async ({ page, request }) => {
  const list = await (await request.get("/api/v1/incidents?limit=50")).json();
  expect(list.total).toBeGreaterThan(0);
  await page.goto("/");
  await expect(page.getByText(`${list.total} incident`)).toBeVisible();
  const resolved = list.items.find((i: { status: string }) => i.status === "RESOLVED") ?? list.items[0];
  await page.goto(`/incidents/${resolved.id}`);
  await expect(page.getByRole("heading", { level: 1 })).toContainText(resolved.service);
  const evidence = page.getByRole("button", { name: /^Open evidence / }).first();
  if (await evidence.count()) {
    await evidence.click();
    await expect(page.getByRole("dialog", { name: /Evidence/ }).getByText("Content hash")).toBeVisible();
  }
  await page.goto("/operations");
  await expect(page.getByText("Dead letters")).toBeVisible();
});
