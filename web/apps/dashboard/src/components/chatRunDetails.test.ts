import { describe, expect, it } from "vitest";
import type { HandoffScopes, SubagentRun } from "../api";
import { parseDagArgs, subagentRunDetails } from "./chatRunDetails";

const scopes: HandoffScopes = {
  bucket: "workspace",
  mode: "read_write",
  allow_patterns: ["src/**"],
  deny_patterns: [],
  outputs_prefix: "outputs/",
  write_prefixes: ["src/generated/"],
  ttl_seconds: 120,
};

describe("chat run details helpers", () => {
  it("parses only object-shaped DAG arguments", () => {
    expect(parseDagArgs('{"goal":"ship","count":2}')).toEqual({
      goal: "ship",
      count: 2,
    });
    expect(parseDagArgs("[1,2,3]")).toEqual({});
    expect(parseDagArgs("{bad json")).toEqual({});
  });

  it("builds compact subagent run details from the latest result event", () => {
    const run = {
      grant_id: "grant-1234567890abcdef",
      rerun_of_grant_id: "grant-original-abcdef",
      thread_id: "thread-1",
      agent_name: "reviewer",
      skill_name: "audit",
      args_json: '{"path":"src/App.tsx"}',
      scopes,
      status: "failed",
      summary: "policy blocked",
      error: "explicit backend error",
      file_ops: [],
      created_at: "2026-06-19T12:00:00.000Z",
      updated_at: "2026-06-19T12:01:00.000Z",
      completed_at: "2026-06-19T12:01:30.000Z",
      handoffs: [
        {
          id: "handoff-1",
          from_agent: "main",
          to_agent: "reviewer",
          skill: "audit",
          grant_id: "grant-1234567890abcdef",
          status: "failed",
          summary: "policy blocked",
          scopes,
          args_preview: { path: "src/App.tsx" },
          created_at: "2026-06-19T12:00:00.000Z",
          completed_at: "2026-06-19T12:01:30.000Z",
        },
      ],
      grants: [],
      receipts: [],
      events: [
        {
          id: 1,
          event_type: "partial",
          payload: { result: { stale: true } },
          created_at: "2026-06-19T12:00:30.000Z",
        },
        {
          id: 2,
          event_type: "complete",
          payload: { payload: { result: { ok: false, finding_count: 3 } } },
          created_at: "2026-06-19T12:01:30.000Z",
        },
      ],
    } satisfies SubagentRun & { error: string };

    const details = subagentRunDetails(run);

    expect(details).toMatchObject({
      kind: "subagent",
      id: "grant-1234567890abcdef",
      title: "reviewer.audit",
      subtitle: "rerun of grant-origin",
      status: "failed",
      summary: "policy blocked",
      error: "explicit backend error",
      args: { path: "src/App.tsx" },
      result: { ok: false, finding_count: 3 },
    });
    expect(details.stats?.map((stat) => stat.label)).toEqual([
      "grant",
      "thread",
      "handoffs",
      "grants",
      "receipts",
      "updated",
    ]);
    expect(details.original_request).toMatchObject({
      agent_name: "reviewer",
      skill_name: "audit",
      grant_id: "grant-1234567890abcdef",
      args: { path: "src/App.tsx" },
      scopes,
    });
    expect(details.events).toHaveLength(2);
  });
});
