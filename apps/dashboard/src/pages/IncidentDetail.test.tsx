import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { defaultHandler, fixtures, json, mockApi, renderAt } from "../test/server";

const d = fixtures.details;

describe("incident detail", () => {
  it("renders a resolved incident: header, RCA, remediation and passed verification", async () => {
    mockApi();
    renderAt(`/incidents/${d.resolved.incident.id}`);
    expect(await screen.findByRole("heading", { level: 1 })).toHaveTextContent(d.resolved.incident.service);
    const verification = screen.getByRole("region", { name: "Verification" });
    expect(within(verification).getAllByText("Passed").length).toBeGreaterThan(0);
    const remediation = screen.getByRole("region", { name: "Remediation" });
    expect(within(remediation).getByText(d.resolved.remediations[0].remediation.action_id)).toBeInTheDocument();
    expect(within(remediation).getByText("Executed")).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Root cause" })).toBeInTheDocument();
  });

  it("renders a failed verification with its reason and failing checks", async () => {
    mockApi();
    renderAt(`/incidents/${d.verificationFailed.incident.id}`);
    const verification = await screen.findByRole("region", { name: "Verification" });
    expect(within(verification).getAllByText("Failed").length).toBeGreaterThan(0);
    expect(within(verification).getByText(/consecutive successful observations/)).toBeInTheDocument();
    expect(within(verification).getByText("health.status")).toBeInTheDocument();
    expect(within(verification).getByText(/next: reinvestigate/)).toBeInTheDocument();
  });

  it("renders the timeline in order and condenses evidence steps", async () => {
    mockApi();
    renderAt(`/incidents/${d.resolved.incident.id}`);
    const timeline = await screen.findByRole("region", { name: "Timeline" });
    const items = within(timeline).getAllByRole("listitem");
    expect(items).toHaveLength(d.resolved.timeline.length);
    expect(items[0]).toHaveTextContent(d.resolved.timeline[0].title);
    await userEvent.click(within(timeline).getByRole("switch"));
    const condensed = within(timeline).getAllByRole("listitem");
    const keyEvents = d.resolved.timeline.filter((e) => e.kind !== "evidence" && e.kind !== "hypothesis");
    expect(condensed).toHaveLength(keyEvents.length);
  });

  it("opens the evidence record behind an evidence link", async () => {
    const { calls } = mockApi();
    renderAt(`/incidents/${d.resolved.incident.id}`);
    const timeline = await screen.findByRole("region", { name: "Timeline" });
    const link = within(timeline).getAllByRole("button", { name: /^Open evidence / })[0];
    await userEvent.click(link);
    await waitFor(() => expect(calls.some((c) => c.url.pathname.startsWith("/api/v1/evidence/"))).toBe(true));
    const dialog = await screen.findByRole("dialog", { name: /^Evidence / });
    expect(await within(dialog).findByText(fixtures.evidence.summary)).toBeInTheDocument();
  });

  it("offers an approval only when one is pending, bound to the proposal hash", async () => {
    const { calls } = mockApi((url, init) => {
      if (init?.method === "POST") return json({ ...d.awaiting.remediations[0].remediation, status: "APPROVED" });
      return defaultHandler(url);
    });
    renderAt(`/incidents/${d.awaiting.incident.id}`);
    await userEvent.click(await screen.findByRole("button", { name: "Review and decide" }));
    const dialog = await screen.findByRole("dialog", { name: /^Remediation / });
    const submit = within(dialog).getByRole("button", { name: "Approve this exact proposal" });
    expect(submit).toBeDisabled();
    await userEvent.type(within(dialog).getByLabelText("Operator API token"), "t0ken");
    await userEvent.type(within(dialog).getByLabelText(/Approver/), "alice");
    await userEvent.click(submit);
    await waitFor(() => expect(calls.some((c) => c.init?.method === "POST")).toBe(true));
    const post = calls.find((c) => c.init?.method === "POST")!;
    const r = d.awaiting.remediations[0].remediation;
    expect(post.url.pathname).toBe(`/api/v1/remediations/${r.id}/approval`);
    expect((post.init!.headers as Record<string, string>).Authorization).toBe("Bearer t0ken");
    expect(JSON.parse(String(post.init!.body))).toMatchObject({
      approver: "alice",
      decision: "approve",
      proposal_hash: r.proposal_hash,
      policy_decision_id: r.policy_decision_id,
    });
  });

  it("shows the API's refusal when an approval is rejected", async () => {
    mockApi((url, init) => (init?.method === "POST" ? json({ detail: "stale proposal hash" }, 409) : defaultHandler(url)));
    renderAt(`/incidents/${d.awaiting.incident.id}`);
    await userEvent.click(await screen.findByRole("button", { name: "Review and decide" }));
    const dialog = await screen.findByRole("dialog", { name: /^Remediation / });
    await userEvent.type(within(dialog).getByLabelText("Operator API token"), "t");
    await userEvent.type(within(dialog).getByLabelText(/Approver/), "alice");
    await userEvent.click(within(dialog).getByRole("button", { name: "Approve this exact proposal" }));
    expect(await within(dialog).findByText(/409 stale proposal hash/)).toBeInTheDocument();
  });

  it("has no approval action for a resolved incident", async () => {
    mockApi();
    renderAt(`/incidents/${d.resolved.incident.id}`);
    await screen.findByRole("region", { name: "Verification" });
    expect(screen.queryByRole("button", { name: "Review and decide" })).toBeNull();
  });

  it("shows empty panels for an incident still triaging", async () => {
    mockApi();
    renderAt(`/incidents/${d.triaging.incident.id}`);
    expect(await screen.findByText("No investigation has started.")).toBeInTheDocument();
    expect(screen.getByText(/No remediation proposed/)).toBeInTheDocument();
    expect(screen.getByText(/Nothing to verify yet/)).toBeInTheDocument();
  });

  it("shows a not-found error", async () => {
    mockApi();
    renderAt("/incidents/00000000-0000-0000-0000-000000000000");
    expect(await screen.findByRole("alert")).toHaveTextContent("incident not found");
  });
});
