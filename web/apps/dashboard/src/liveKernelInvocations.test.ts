import { describe, expect, it } from "vitest";
import { formatElapsed, parseLiveInvocations, previewValue } from "./liveKernelInvocations";
import type { ProtocolSimulation } from "./api";

function runWithLive(live_invocations: Record<string, unknown>): ProtocolSimulation {
  return {
    job: {
      id: 1,
      job_id: "uksim-test",
      kind: "protocol_simulation",
      title: "Kernel test",
      status: "failed",
      summary: null,
      error: null,
      thread_id: null,
      metadata: {},
      created_at: "",
      updated_at: "",
      started_at: null,
      completed_at: null,
      result: { live_invocations },
    },
    events: [],
  };
}

describe("parseLiveInvocations", () => {
  it("normalizes live invocation records with timeout diagnostics", () => {
    const live = parseLiveInvocations(
      runWithLive({
        passed: false,
        count: 2,
        fail_count: 1,
        digest: "digest-1",
        records: [
          {
            node_id: "openpannel",
            agent_name: "openpannel",
            skill_name: "auto",
            role: "lead_partner",
            status: "passed",
            elapsed_ms: 85439,
            arguments: { goal: "compare agents" },
            result: { final: { score: 95, summary: "real evidence" } },
          },
          {
            node_id: "apify-swarm-agent",
            agent_name: "apify-swarm-agent",
            skill_name: "run_swarm",
            role: "partner",
            status: "failed",
            elapsed_ms: 130008,
            error: "ReadTimeout:",
            arguments: { query: "research" },
            result: {},
          },
        ],
      }),
    );

    expect(live?.passed).toBe(false);
    expect(live?.count).toBe(2);
    expect(live?.failCount).toBe(1);
    expect(live?.digest).toBe("digest-1");
    expect(live?.records[0]).toEqual(
      expect.objectContaining({
        agentName: "openpannel",
        skillName: "auto",
        status: "passed",
        elapsedLabel: "1m 25s",
        timeoutLikely: false,
      }),
    );
    expect(live?.records[0].resultPreview).toContain("real evidence");
    expect(live?.records[1]).toEqual(
      expect.objectContaining({
        agentName: "apify-swarm-agent",
        status: "failed",
        error: "ReadTimeout:",
        elapsedLabel: "2m 10s",
        timeoutLikely: true,
      }),
    );
  });

  it("returns null for missing live payloads", () => {
    expect(parseLiveInvocations(null)).toBeNull();
    expect(parseLiveInvocations(runWithLive({ records: [] }))?.records).toEqual([]);
  });
});

describe("live invocation formatting", () => {
  it("formats elapsed durations", () => {
    expect(formatElapsed(null)).toBe("-");
    expect(formatElapsed(250)).toBe("250ms");
    expect(formatElapsed(1420)).toBe("1s");
    expect(formatElapsed(32926)).toBe("33s");
    expect(formatElapsed(130008)).toBe("2m 10s");
  });

  it("truncates long previews", () => {
    const preview = previewValue({ text: "x".repeat(200) }, 40);
    expect(preview.length).toBeLessThanOrEqual(42);
    expect(preview.endsWith("...")).toBe(true);
  });
});
