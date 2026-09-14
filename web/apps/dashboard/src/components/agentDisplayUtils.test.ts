import { describe, expect, it } from "vitest";
import { fmtDate, statusColor, summarizeRuns } from "./agentDisplayUtils";

describe("agent display utilities", () => {
  it("summarizes failed or denied runs", () => {
    expect(
      summarizeRuns([
        { status: "complete" },
        { status: "error" },
        { status: "running" },
        { status: "denied" },
      ]),
    ).toEqual({ failures: 2 });
  });

  it("maps statuses to dashboard tone classes", () => {
    expect(statusColor("complete")).toBe("text-signal-live");
    expect(statusColor("error")).toBe("text-signal-danger");
    expect(statusColor("denied")).toBe("text-signal-danger");
    expect(statusColor("running")).toBe("text-signal-peer");
    expect(statusColor("queued")).toBe("text-ink-muted");
  });

  it("guards invalid dates", () => {
    expect(fmtDate("not-a-date")).toBe("unknown");
  });
});
