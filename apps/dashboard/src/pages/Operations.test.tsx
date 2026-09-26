import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { defaultHandler, fixtures, json, mockApi, renderAt } from "../test/server";

describe("operations overview", () => {
  it("renders counters, escalations and metrics from the API", async () => {
    mockApi();
    renderAt("/operations");
    expect(await screen.findByText("Dead letters")).toBeInTheDocument();
    const escalations = screen.getByRole("region", { name: "Recent escalations" });
    expect(within(escalations).getAllByRole("link")).toHaveLength(fixtures.overview.recent_escalations.length);
    expect(await screen.findByText("Verification latency")).toBeInTheDocument();
  });

  it("reports live workers and an unreachable Redis honestly", async () => {
    mockApi((url) =>
      url.pathname === "/api/v1/overview"
        ? json({ ...fixtures.overview, redis_available: false, workers: [] })
        : defaultHandler(url),
    );
    renderAt("/operations");
    expect(await screen.findByText("Redis unreachable")).toBeInTheDocument();
  });
});
