import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { fixtures, json, mockApi, renderAt } from "../test/server";

describe("incident list", () => {
  it("renders every incident from the API with status and remediation state", async () => {
    mockApi();
    renderAt("/");
    const table = await screen.findByRole("table", { name: "Incidents" });
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows).toHaveLength(fixtures.incidents.items.length);
    expect(screen.getByText(/10 incidents · 4 active in view/)).toBeInTheDocument();
    const failed = rows[0];
    expect(within(failed).getByRole("link", { name: /6794cfbf/ })).toHaveAttribute("href", "/incidents/6794cfbf-e8dd-4119-93c2-f4005600a100");
    expect(within(failed).getAllByText("Verification failed").length).toBeGreaterThan(0);
    expect(within(failed).getByText("Executed")).toBeInTheDocument();
  });

  it("sends the chosen filters to the API", async () => {
    const { calls } = mockApi();
    renderAt("/");
    await screen.findByRole("table", { name: "Incidents" });
    await userEvent.selectOptions(screen.getByLabelText("Status"), "RESOLVED");
    await userEvent.selectOptions(screen.getByLabelText("Severity"), "critical");
    await waitFor(() => {
      const last = calls.at(-1)!.url.searchParams;
      expect(last.get("status")).toBe("RESOLVED");
      expect(last.get("severity")).toBe("critical");
    });
  });

  it("restores filters from the URL", async () => {
    const { calls } = mockApi();
    renderAt("/?service=payment-service&range=24h");
    await screen.findByRole("table", { name: "Incidents" });
    const q = calls[0].url.searchParams;
    expect(q.get("service")).toBe("payment-service");
    expect(q.get("since")).not.toBeNull();
    expect(screen.getByLabelText("Opened")).toHaveValue("24h");
  });

  it("shows an empty state", async () => {
    mockApi(() => json({ total: 0, items: [], services: [] }));
    renderAt("/");
    expect(await screen.findByText(/No incidents yet/)).toBeInTheDocument();
  });

  it("shows a filter-specific empty state", async () => {
    mockApi(() => json({ total: 0, items: [], services: [] }));
    renderAt("/?status=CANCELLED");
    expect(await screen.findByText("No incidents match these filters.")).toBeInTheDocument();
  });

  it("shows an error with retry when the API fails", async () => {
    let fail = true;
    mockApi(() => (fail ? json({ detail: "database unavailable" }, 503) : json(fixtures.incidents)));
    renderAt("/");
    expect(await screen.findByRole("alert")).toHaveTextContent("database unavailable");
    fail = false;
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByRole("table", { name: "Incidents" })).toBeInTheDocument();
  });

  it("shows a loading skeleton before data arrives", async () => {
    mockApi(() => new Promise<Response>(() => {}));
    const { container } = renderAt("/");
    expect(container.querySelector(".cds--data-table.cds--skeleton, .cds--skeleton")).not.toBeNull();
  });
});
