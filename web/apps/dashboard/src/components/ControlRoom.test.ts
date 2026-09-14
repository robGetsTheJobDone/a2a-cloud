import { describe, expect, it } from "vitest";
import type { ControlTimelineItem } from "../api";
import {
  buildControlFailureReviewQueue,
  filterControlTimeline,
} from "./ControlRoom";

function receipt(
  id: string,
  overrides: Partial<ControlTimelineItem> = {},
): ControlTimelineItem {
  return {
    id,
    source: "dag",
    title: `Receipt ${id}`,
    status: "completed",
    summary: null,
    agent_name: null,
    skill_name: null,
    cost_cents: 0,
    token_count: 0,
    file_ops: [],
    receipt_path: `receipts/${id}.json`,
    created_at: "2026-06-01T00:00:00.000Z",
    completed_at: null,
    metadata: {},
    ...overrides,
  };
}

describe("ControlRoom failure review helpers", () => {
  it("filters the runtime ledger to failed receipts inside the selected source", () => {
    const items = [
      receipt("dag-failed", { status: "failed", source: "dag" }),
      receipt("proof-denied", { status: "denied", source: "proof" }),
      receipt("dag-ok", { status: "completed", source: "dag" }),
      receipt("llm-error", { status: "error", source: "llm" }),
    ];

    expect(filterControlTimeline(items, "all", "failed").map((item) => item.id))
      .toEqual(["dag-failed", "proof-denied", "llm-error"]);
    expect(filterControlTimeline(items, "proof", "failed").map((item) => item.id))
      .toEqual(["proof-denied"]);
  });

  it("sorts the failure review queue newest first and reports missing failures", () => {
    const queue = buildControlFailureReviewQueue(
      [
        receipt("older", {
          status: "failed",
          created_at: "2026-06-01T00:00:00.000Z",
        }),
        receipt("newer", {
          status: "error",
          created_at: "2026-06-02T00:00:00.000Z",
        }),
        receipt("ok", {
          status: "completed",
          created_at: "2026-06-03T00:00:00.000Z",
        }),
      ],
      4,
      1,
    );

    expect(queue.failures.map((item) => item.id)).toEqual(["newer"]);
    expect(queue.loadedFailures).toBe(2);
    expect(queue.totalFailures).toBe(4);
    expect(queue.missingFailures).toBe(2);
  });

  it("keeps loaded failures visible even when the summary has not caught up", () => {
    const queue = buildControlFailureReviewQueue(
      [receipt("latest-failure", { status: "FAILED" })],
      0,
    );

    expect(queue.totalFailures).toBe(1);
    expect(queue.loadedFailures).toBe(1);
    expect(queue.missingFailures).toBe(0);
  });
});
