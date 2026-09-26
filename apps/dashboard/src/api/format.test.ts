import { describe, expect, it } from "vitest";

import { incidentQuery } from "./client";
import { clock, duration, label, metricValue, percent, shortId, tone } from "./format";

describe("formatting", () => {
  it("labels enum values as sentence case, keeping acronyms", () => {
    expect(label("VERIFICATION_FAILED")).toBe("Verification failed");
    expect(label("RCA_READY")).toBe("RCA ready");
    expect(label("cpu_usage")).toBe("CPU usage");
    expect(label(null)).toBe("—");
  });

  it("maps statuses to tones", () => {
    expect(tone("RESOLVED")).toBe("green");
    expect(tone("VERIFICATION_FAILED")).toBe("red");
    expect(tone("AWAITING_APPROVAL")).toBe("yellow");
    expect(tone("firing")).toBe("red");
    expect(tone("resolved")).toBe("green");
  });

  it("formats durations, ids, clocks and metrics", () => {
    expect(duration(0.2)).toBe("200ms");
    expect(duration(635)).toBe("10m 35s");
    expect(duration(null)).toBe("—");
    expect(shortId("6794cfbf-e8dd-4119-93c2-f4005600a100")).toBe("6794cfbf");
    expect(clock("2026-09-26T17:48:47.123+00:00")).toBe("17:48:47");
    expect(percent(0.32)).toBe("32.0%");
    expect(metricValue("error_rate", 0.05)).toContain("%");
  });
});

describe("incidentQuery", () => {
  it("serialises filters into the API's query parameters", () => {
    const now = new Date("2026-09-26T12:00:00Z");
    const q = new URLSearchParams(incidentQuery({ status: "RESOLVED", severity: "critical", service: "checkout-service", range: "1h" }, now));
    expect(q.get("status")).toBe("RESOLVED");
    expect(q.get("severity")).toBe("critical");
    expect(q.get("service")).toBe("checkout-service");
    expect(q.get("since")).toBe("2026-09-26T11:00:00.000Z");
    expect(q.get("limit")).toBe("200");
  });

  it("omits unset filters", () => {
    const q = new URLSearchParams(incidentQuery({}));
    expect([...q.keys()]).toEqual(["limit"]);
  });
});
