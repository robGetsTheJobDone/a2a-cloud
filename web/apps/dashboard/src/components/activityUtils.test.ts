import { describe, expect, it } from "vitest";
import type { Page, WorkEvent, WorkJob } from "../api";
import {
  activityFilterBadges,
  buildOptions,
  compactPayload,
  detailError,
  eventKey,
  eventMessage,
  jobChips,
  jobDuration,
  jobTitle,
  mergeJobDetail,
  normalizeEventPage,
  parseActivityUrlFilters,
  statusFamily,
  summarizeActivity,
  uniqueJobs,
} from "./activityUtils";

const baseJob: WorkJob = {
  id: 1,
  job_id: "job-1",
  kind: "trial",
  title: null,
  status: "running",
  summary: null,
  error: null,
  thread_id: null,
  metadata: {},
  created_at: "2026-06-19T10:00:00.000Z",
  updated_at: "2026-06-19T10:01:00.000Z",
  started_at: "2026-06-19T10:00:00.000Z",
  completed_at: null,
};

const baseEvent: WorkEvent = {
  id: 1,
  job_id: "job-1",
  event_id: "event-1",
  event_type: "started",
  payload: {},
  created_at: "2026-06-19T10:00:00.000Z",
};

function job(overrides: Partial<WorkJob>): WorkJob {
  return { ...baseJob, ...overrides };
}

function event(overrides: Partial<WorkEvent>): WorkEvent {
  return { ...baseEvent, ...overrides };
}

describe("activity utilities", () => {
  it("parses activity deep-link filters and exposes compact badges", () => {
    const filters = parseActivityUrlFilters(
      "?agent=billing-agent&source=proof&grant_id=grant-1&thread=thread-9&status=all&q=review",
    );

    expect(filters).toEqual({
      agent: "billing-agent",
      grant: "grant-1",
      q: "review",
      source: "proof",
      thread_id: "thread-9",
    });
    expect(activityFilterBadges(filters)).toEqual([
      { key: "agent", label: "agent", value: "billing-agent" },
      { key: "source", label: "source", value: "proof" },
      { key: "thread_id", label: "thread", value: "thread-9" },
      { key: "grant", label: "grant", value: "grant-1" },
      { key: "q", label: "search", value: "review" },
    ]);
  });

  it("summarizes statuses and unique sources", () => {
    expect(
      summarizeActivity([
        job({ job_id: "queued", status: "queued", source: "agent" }),
        job({ job_id: "done", status: "succeeded", source: "agent" }),
        job({ job_id: "failed", status: "cancelled", kind: "deployment" }),
      ]),
    ).toEqual({ active: 1, done: 1, failed: 1, sources: 2 });

    expect(statusFamily("unknown")).toBe("other");
  });

  it("normalizes legacy and paged event responses", () => {
    const itemsPage: Page<WorkEvent> = {
      items: [event({ event_id: "event-items" })],
      next_cursor: "next",
    };

    expect(normalizeEventPage([event({ event_id: "event-array" })])).toEqual({
      items: [event({ event_id: "event-array" })],
      nextCursor: null,
    });
    expect(normalizeEventPage(itemsPage)).toEqual({
      items: itemsPage.items,
      nextCursor: "next",
    });
    expect(
      normalizeEventPage({
        events: [event({ event_id: "event-legacy" })],
        next_cursor: null,
      }),
    ).toEqual({
      items: [event({ event_id: "event-legacy" })],
      nextCursor: null,
    });
  });

  it("deduplicates jobs while preserving the first item", () => {
    expect(
      uniqueJobs([
        job({ id: 1, job_id: "job-a", title: "First" }),
        job({ id: 2, job_id: "job-b", title: "Second" }),
        job({ id: 3, job_id: "job-a", title: "Duplicate" }),
      ]).map((item) => item.title),
    ).toEqual(["First", "Second"]);
  });

  it("keeps fallback source when merging sparse job detail", () => {
    expect(
      mergeJobDetail(
        job({ job_id: "job-1", kind: "proof", source: "" }),
        job({ job_id: "job-1", kind: "trial", source: "agent" }),
      )["source"],
    ).toBe("agent");
  });

  it("builds compact labels, chips, and payload previews", () => {
    const longGrant = "grant-" + "x".repeat(80);
    const current = job({
      job_id: "job-chip",
      kind: "llm-call",
      title: "  ",
      agent_name: "billing-agent",
      grant_id: longGrant,
    });

    expect(jobTitle(current)).toBe("llm call job");
    expect(jobChips(current)).toContainEqual({
      label: "agent",
      value: "billing-agent",
    });
    expect(jobChips(current).find((chip) => chip.label === "grant")?.value).toMatch(/\.\.\.$/);
    expect(compactPayload({ value: "x".repeat(500) })).toMatch(/\.\.\.$/);
  });

  it("formats durations and event display values", () => {
    expect(
      jobDuration(
        job({
          started_at: "2026-06-19T10:00:00.000Z",
          completed_at: "2026-06-19T10:02:00.000Z",
        }),
      ),
    ).toBe("2m");

    const current = event({
      event_id: "",
      message: null,
      payload: { summary: "queued worker" },
    });
    expect(eventKey(current)).toBe("job-1:1");
    expect(eventMessage(current)).toBe("queued worker");
  });

  it("combines detail loading errors", () => {
    expect(
      detailError(
        { status: "rejected", reason: new Error("missing job") },
        { status: "rejected", reason: "events unavailable" },
      ),
    ).toBe("Job: missing job Events: events unavailable");

    expect(buildOptions(["queued"], ["running", "queued"], "failed")).toEqual([
      "queued",
      "running",
      "failed",
    ]);
  });
});
