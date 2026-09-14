import { describe, expect, it } from "vitest";
import type { TrialRoom, TrialRun } from "../api";
import {
  changedFileCount,
  fmtBytes,
  fmtMoney,
  fmtMs,
  isTrialRunnableAgent,
  mergeTrialRoomList,
  opTone,
  outputCoverage,
  receiptId,
  schemaRequiredKeys,
  summarizeTrialAgentReadiness,
  stringReceiptValue,
  summarizeRooms,
  summarizeRunComparison,
} from "./trialRoomUtils";

const baseRun: TrialRun = {
  id: 1,
  agent_id: 10,
  agent_name: "ledger-agent",
  skill_name: "reconcile",
  grant_id: null,
  status: "passed",
  score: 80,
  summary: null,
  evaluator_notes: null,
  error: null,
  args_preview: {},
  result: { summary: "done" },
  events: [],
  file_ops: [],
  receipt_json: {},
  elapsed_ms: 1500,
  created_at: "2026-06-19T10:00:00.000Z",
  started_at: "2026-06-19T10:00:00.000Z",
  completed_at: "2026-06-19T10:00:01.500Z",
};

const baseRoom: TrialRoom = {
  id: 1,
  slug: "room-1",
  title: "Room 1",
  goal: "Reconcile invoices",
  acceptance_criteria: "",
  input_paths: ["invoices.csv"],
  output_schema: {
    type: "object",
    required: ["summary", " total ", ""],
  },
  max_cost_cents: 500,
  max_runtime_seconds: 300,
  status: "open",
  selected_run_id: null,
  deployed_agent_id: null,
  created_at: "2026-06-19T10:00:00.000Z",
  updated_at: "2026-06-19T10:00:00.000Z",
  runs: [],
};

function run(overrides: Partial<TrialRun>): TrialRun {
  return { ...baseRun, ...overrides };
}

function room(overrides: Partial<TrialRoom>): TrialRoom {
  return { ...baseRoom, ...overrides };
}

function agent(overrides: {
  public?: boolean;
  status?: string;
  skills?: unknown[];
} = {}) {
  return {
    public: overrides.public ?? true,
    status: overrides.status ?? "running",
    card: {
      skills: overrides.skills ?? [{ name: "reconcile" }],
    },
  };
}

describe("trial room utilities", () => {
  it("summarizes room and comparison totals", () => {
    const first = run({
      id: 1,
      score: 92,
      status: "passed",
      file_ops: [
        { op: "create", path: "a.txt", size: 10 },
        { op: "delete", path: "old.txt", size: 0 },
      ],
    });
    const second = run({
      id: 2,
      score: 75,
      status: "failed",
      file_ops: [{ op: "update", path: "b.txt", size: 20 }],
    });

    expect(
      summarizeRooms([
        room({ status: "deployed", runs: [first] }),
        room({ id: 2, slug: "room-2", status: "open", runs: [second] }),
      ]),
    ).toEqual({ total: 2, runs: 2, deployed: 1 });

    expect(summarizeRunComparison([first, second])).toEqual({
      bestScore: 92,
      scoreSpread: 17,
      passedRuns: 1,
      artifacts: 2,
    });
  });

  it("merges fetched rooms without dropping the current list", () => {
    const first = room({ slug: "first", title: "First" });
    const second = room({ id: 2, slug: "second", title: "Second" });
    const updatedSecond = room({ id: 2, slug: "second", title: "Second updated" });
    const older = room({ id: 3, slug: "older", title: "Older direct link" });

    expect(mergeTrialRoomList([first, second], updatedSecond)).toEqual([
      first,
      updatedSecond,
    ]);
    expect(mergeTrialRoomList([first, second], older)).toEqual([
      older,
      first,
      second,
    ]);
  });

  it("reports output schema coverage", () => {
    const currentRoom = room({});
    const currentRun = run({ result: { summary: "ok" } });

    expect(schemaRequiredKeys(currentRoom.output_schema)).toEqual(["summary", "total"]);
    expect(outputCoverage(currentRoom, currentRun)).toEqual({
      required: ["summary", "total"],
      present: ["summary"],
      missing: ["total"],
    });
  });

  it("classifies trial candidate agent readiness", () => {
    expect(isTrialRunnableAgent(agent())).toBe(true);
    expect(summarizeTrialAgentReadiness([])).toMatchObject({
      state: "empty",
      runnable: 0,
      total: 0,
    });
    expect(
      summarizeTrialAgentReadiness([
        agent({ public: true, status: "running", skills: [] }),
      ]),
    ).toMatchObject({
      state: "missing_skills",
      publicRunning: 1,
      publicRunningWithoutSkills: 1,
    });
    expect(
      summarizeTrialAgentReadiness([
        agent({ public: false, status: "running" }),
        agent({ public: true, status: "deploying" }),
      ]),
    ).toMatchObject({
      state: "private_running",
      privateRunning: 1,
      publicOffline: 1,
    });
    expect(
      summarizeTrialAgentReadiness([
        agent({ public: true, status: "stopped" }),
      ]),
    ).toMatchObject({
      state: "public_offline",
      publicOffline: 1,
    });
    expect(summarizeTrialAgentReadiness([agent()])).toMatchObject({
      state: "ready",
      runnable: 1,
    });
  });

  it("extracts receipt identifiers and nested receipt values", () => {
    const current = run({
      receipt_json: {
        receipt_id: "receipt-123",
        input_set_hash: "hash-abc",
        nested: { value: "inner" },
      },
    });

    expect(receiptId(current)).toBe("receipt-123");
    expect(stringReceiptValue(current, ["nested", "value"])).toBe("inner");
    expect(stringReceiptValue(current, ["missing"])).toBe("");
  });

  it("formats compact dashboard values", () => {
    expect(changedFileCount(run({
      file_ops: [
        { op: "create", path: "a", size: 1 },
        { op: "update", path: "b", size: 1 },
        { op: "delete", path: "c", size: 1 },
      ],
    }))).toBe(2);
    expect(fmtMoney(0)).toBe("$0");
    expect(fmtMoney(1250)).toBe("$13");
    expect(fmtMs(null)).toBe("-");
    expect(fmtMs(950)).toBe("950ms");
    expect(fmtMs(1500)).toBe("1.5s");
    expect(fmtBytes(100)).toBe("100 B");
    expect(fmtBytes(2048)).toBe("2.0 KB");
    expect(opTone("delete")).toContain("text-signal-danger");
  });
});
