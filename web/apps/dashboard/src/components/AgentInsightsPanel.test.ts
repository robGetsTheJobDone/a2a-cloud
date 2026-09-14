import { describe, expect, it } from "vitest";
import type { AgentCallLog } from "../api";
import { agentLogDetails } from "./AgentInsightsPanel";

describe("agentLogDetails", () => {
  it("maps platform call logs into run detail panels", () => {
    const log = {
      id: "log-1",
      source: "handoff",
      agent_name: "researcher",
      skill_name: "summarize",
      status: "passed",
      badge: "verified",
      summary: "summarized repo",
      error: null,
      grant_id: "grant-1234567890abcdef",
      args_preview: { path: "src/App.tsx" },
      result_preview: { ok: true },
      events: [
        { event_type: "started", step: 1 },
        { type: "complete", ok: true },
        { payload: "fallback" },
      ],
      file_ops: [],
      elapsed_ms: 1520,
      created_at: "2026-06-19T12:00:00.000Z",
      started_at: "2026-06-19T12:00:01.000Z",
      completed_at: "2026-06-19T12:00:02.520Z",
      metadata: { receipt: "present" },
    } satisfies AgentCallLog;

    const details = agentLogDetails(log);

    expect(details).toMatchObject({
      kind: "agent-log",
      id: "log-1",
      title: "researcher.summarize",
      subtitle: "handoff",
      status: "passed",
      summary: "summarized repo",
      error: null,
      args: { path: "src/App.tsx" },
      result: { ok: true },
      receipt: {
        badge: "verified",
        metadata: { receipt: "present" },
        created_at: "2026-06-19T12:00:00.000Z",
      },
    });
    expect(details.stats).toEqual([
      { label: "source", value: "handoff" },
      { label: "grant", value: "grant-123456" },
      { label: "elapsed", value: "1.5s" },
    ]);
    expect(details.events?.map((event) => event.type)).toEqual([
      "started",
      "complete",
      "event-3",
    ]);
  });
});
