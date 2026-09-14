import { describe, expect, it } from "vitest";
import type { AgentProofRun, SubagentRun } from "../api";
import { summarizeAgentInsights } from "./AgentInsightsDashboard";

const now = "2026-06-19T12:00:00.000Z";

describe("summarizeAgentInsights", () => {
  it("counts compact run and proof file operation totals", () => {
    const run = {
      grant_id: "grant-1",
      rerun_of_grant_id: null,
      thread_id: null,
      agent_name: "alpha",
      skill_name: "build",
      args_json: "{}",
      scopes: {
        bucket: "bucket",
        mode: "read_write_overlay",
        allow_patterns: ["**"],
        deny_patterns: [],
        outputs_prefix: null,
        write_prefixes: [],
        ttl_seconds: 300,
      },
      status: "passed",
      summary: "ok",
      file_ops: [],
      file_ops_count: 2,
      created_at: now,
      updated_at: now,
      completed_at: now,
      events: [],
    } satisfies SubagentRun;
    const proof = {
      id: 1,
      agent_name: "alpha",
      skill_name: "prove",
      grant_id: "grant-2",
      status: "passed",
      badge: "verified",
      summary: "ok",
      error: null,
      args_preview: {},
      result: {},
      events: [],
      file_ops: [],
      file_ops_count: 3,
      card_hash: null,
      repo_url: null,
      head_sha: null,
      image: null,
      agent_url: null,
      elapsed_ms: 25,
      created_at: now,
      started_at: now,
      completed_at: now,
    } satisfies AgentProofRun;

    const summary = summarizeAgentInsights("alpha", [], [run], [proof]);

    expect(summary.fileOpsTotal).toBe(5);
    expect(summary.uniqueFiles).toBe(0);
  });
});
