import type { Page, WorkEvent, WorkJob } from "../api";

const ACTIVE_STATUSES = new Set(["queued", "waiting", "pending", "running"]);
const DONE_STATUSES = new Set([
  "complete",
  "succeeded",
  "success",
  "passed",
  "live",
  "done",
  "ok",
]);
const FAILED_STATUSES = new Set([
  "error",
  "failed",
  "failure",
  "canceled",
  "cancelled",
  "denied",
]);

export type ActivityStatusFamily = "active" | "done" | "failed" | "other";

export type ActivitySummary = {
  active: number;
  done: number;
  failed: number;
  sources: number;
};

export type ActivityUrlFilters = {
  agent?: string;
  grant?: string;
  kind?: string;
  q?: string;
  source?: string;
  status?: string;
  thread_id?: string;
  type?: string;
};

export type ActivityFilterBadge = {
  key: keyof ActivityUrlFilters;
  label: string;
  value: string;
};

export type EventPageLike =
  | Page<WorkEvent>
  | WorkEvent[]
  | {
      items?: WorkEvent[];
      events?: WorkEvent[];
      next_cursor?: string | null;
    };

const ACTIVITY_URL_FILTER_LABELS: Record<keyof ActivityUrlFilters, string> = {
  agent: "agent",
  grant: "grant",
  kind: "kind",
  q: "search",
  source: "source",
  status: "status",
  thread_id: "thread",
  type: "type",
};

const ACTIVITY_URL_FILTER_ORDER: Array<keyof ActivityUrlFilters> = [
  "agent",
  "source",
  "status",
  "kind",
  "type",
  "thread_id",
  "grant",
  "q",
];

export function summarizeActivity(jobs: WorkJob[]): ActivitySummary {
  const sources = new Set<string>();
  let active = 0;
  let done = 0;
  let failed = 0;
  for (const job of jobs) {
    sources.add(jobSource(job));
    const family = statusFamily(job.status);
    if (family === "active") active += 1;
    if (family === "done") done += 1;
    if (family === "failed") failed += 1;
  }
  return { active, done, failed, sources: sources.size };
}

export function parseActivityUrlFilters(search: string): ActivityUrlFilters {
  const params = new URLSearchParams(search);
  return compactActivityFilters({
    agent: paramValue(params, "agent"),
    grant: paramValue(params, "grant") || paramValue(params, "grant_id"),
    kind: paramValue(params, "kind"),
    q: paramValue(params, "q") || paramValue(params, "query") || paramValue(params, "search"),
    source: paramValue(params, "source"),
    status: paramValue(params, "status"),
    thread_id: paramValue(params, "thread_id") || paramValue(params, "thread"),
    type: paramValue(params, "type"),
  });
}

export function activityFilterBadges(filters: ActivityUrlFilters): ActivityFilterBadge[] {
  return ACTIVITY_URL_FILTER_ORDER.flatMap((key) => {
    const value = filters[key];
    if (!value) return [];
    return [{
      key,
      label: ACTIVITY_URL_FILTER_LABELS[key],
      value,
    }];
  });
}

export function statusFamily(status: string): ActivityStatusFamily {
  const normalized = status.toLowerCase();
  if (DONE_STATUSES.has(normalized)) return "done";
  if (FAILED_STATUSES.has(normalized)) return "failed";
  if (ACTIVE_STATUSES.has(normalized)) return "active";
  return "other";
}

export function normalizeEventPage(page: EventPageLike): {
  items: WorkEvent[];
  nextCursor: string | null;
} {
  if (Array.isArray(page)) return { items: page, nextCursor: null };
  const items =
    "events" in page && Array.isArray(page.events)
      ? page.events
      : "items" in page && Array.isArray(page.items)
        ? page.items
        : [];
  return {
    items,
    nextCursor: page.next_cursor ?? null,
  };
}

export function detailError(
  jobResult: PromiseSettledResult<WorkJob>,
  eventResult: PromiseSettledResult<Page<WorkEvent>>,
): string | null {
  const messages: string[] = [];
  if (jobResult.status === "rejected") {
    messages.push(`Job: ${errorText(jobResult.reason)}`);
  }
  if (eventResult.status === "rejected") {
    messages.push(`Events: ${errorText(eventResult.reason)}`);
  }
  return messages.length > 0 ? messages.join(" ") : null;
}

export function mergeJobDetail(detail: WorkJob, fallback: WorkJob | null): WorkJob {
  if (!fallback) return detail;
  return {
    ...fallback,
    ...detail,
    source: stringValue(detail["source"]) || stringValue(fallback["source"]) || detail.kind,
  };
}

export function uniqueJobs(items: WorkJob[]): WorkJob[] {
  const seen = new Set<string>();
  return items.filter((job) => {
    if (seen.has(job.job_id)) return false;
    seen.add(job.job_id);
    return true;
  });
}

export function buildOptions(base: string[], dynamic: string[], selected: string): string[] {
  const values = new Set<string>(base);
  for (const item of dynamic) {
    if (item) values.add(item);
  }
  if (selected !== "all") values.add(selected);
  return Array.from(values);
}

function jobSource(job: WorkJob): string {
  return stringValue(job["source"]) || job.kind || stringValue(job.type) || "work";
}

export function jobTitle(job: WorkJob): string {
  const title = job.title?.trim();
  if (title) return title;
  const kind = job.kind || jobSource(job);
  return `${labelFor(kind)} job`;
}

export function jobChips(job: WorkJob): { label: string; value: string }[] {
  const candidates: [string, unknown][] = [
    ["source", jobSource(job)],
    ["kind", job.kind],
    ["type", job.type],
    ["agent", job.agent_name],
    ["skill", job.skill_name],
    ["thread", job.thread_id],
    ["grant", job.grant_id],
    ["root", job.root_job_id],
    ["parent", job.parent_job_id],
    ["correlation", job.correlation_id],
  ];
  const chips: { label: string; value: string }[] = [];
  for (const [label, raw] of candidates) {
    const value = chipValue(raw);
    if (value && !chips.some((chip) => chip.label === label && chip.value === value)) {
      chips.push({ label, value });
    }
    if (chips.length >= 8) break;
  }
  return chips;
}

export function eventKey(event: WorkEvent): string {
  return event.event_id || `${event.job_id}:${event.id}`;
}

export function eventMessage(event: WorkEvent): string | null {
  return (
    stringValue(event.message) ||
    stringValue(event.payload?.["message"]) ||
    stringValue(event.payload?.["summary"]) ||
    stringValue(event.payload?.["error"]) ||
    stringValue(event["summary"])
  );
}

export function compactPayload(
  payload: Record<string, unknown> | null | undefined,
): string | null {
  if (!payload || Object.keys(payload).length === 0) return null;
  const text = JSON.stringify(payload, null, 2);
  return text.length > 420 ? `${text.slice(0, 420)}...` : text;
}

function chipValue(value: unknown): string | null {
  const text = stringValue(value);
  if (text) return text.length > 42 ? `${text.slice(0, 42)}...` : text;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return null;
}

function stringValue(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

function paramValue(params: URLSearchParams, key: string): string | undefined {
  const value = params.get(key)?.trim();
  return value && value !== "all" ? value : undefined;
}

function compactActivityFilters(filters: ActivityUrlFilters): ActivityUrlFilters {
  const compacted: ActivityUrlFilters = {};
  for (const key of ACTIVITY_URL_FILTER_ORDER) {
    const value = filters[key]?.trim();
    if (value) compacted[key] = value;
  }
  return compacted;
}

function labelFor(value: string): string {
  return value.replace(/[-_]/g, " ");
}

export function jobDuration(job: WorkJob): string | null {
  const start = parseTime(job.started_at || job.created_at);
  const end = parseTime(job.completed_at || job.updated_at);
  if (start === null || end === null || end < start) return null;
  const seconds = Math.max(1, Math.round((end - start) / 1000));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.round(minutes / 60);
  if (hours < 48) return `${hours}h`;
  return `${Math.round(hours / 24)}d`;
}

function parseTime(value: string | null | undefined): number | null {
  if (!value) return null;
  const time = new Date(value).getTime();
  return Number.isNaN(time) ? null : time;
}

function errorText(ex: unknown): string {
  return ex instanceof Error ? ex.message : String(ex);
}
