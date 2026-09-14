import { isLiveStateStatus } from "../DashboardChrome";
import { runtimeControlReceiptHref, type RuntimeViewId } from "../../navigation";
import type { ControlPolicy, ControlSummary, ControlTimelineItem } from "../../api";
import type { StatusPillTone } from "@a2a/design-system";

// ---------------------------------------------------------------------------
// Static option / section tables
// ---------------------------------------------------------------------------

export const SOURCE_OPTIONS = [
  { value: "all", label: "All sources" },
  { value: "dag", label: "DAGs" },
  { value: "subagent", label: "Subagents" },
  { value: "scope", label: "Scope" },
  { value: "llm", label: "LLM" },
  { value: "proof", label: "Proofs" },
  { value: "trial", label: "Trials" },
  { value: "deployment", label: "Deployments" },
  { value: "review_loop", label: "Reviewer loops" },
  { value: "protocol_simulation", label: "Protocol simulations" },
];

export type RuntimeControlViewId = Exclude<RuntimeViewId, "memory">;
export type ControlPolicySection = "overview" | "budgets" | "gates" | "allowlist";
export type ControlReceiptSection = "summary" | "trace" | "payload" | "files" | "metadata";
export type ControlStatusFilter = "all" | "failed";

export const CONTROL_STATUS_OPTIONS: Array<{
  value: ControlStatusFilter;
  label: string;
}> = [
  { value: "all", label: "All statuses" },
  { value: "failed", label: "Failed only" },
];

export const CONTROL_POLICY_SECTIONS: Array<{
  id: ControlPolicySection;
  label: string;
  description: string;
}> = [
  {
    id: "overview",
    label: "Overview",
    description: "Runtime posture, saved policy state, budget caps, active gates, and routing scope.",
  },
  {
    id: "budgets",
    label: "Budgets",
    description: "Monthly spend caps, per-run caps, and maximum agent handoffs.",
  },
  {
    id: "gates",
    label: "Gates",
    description: "Approval, network, allowlist enforcement, and PII-safe mode switches.",
  },
  {
    id: "allowlist",
    label: "Allowlist",
    description: "Approved agent IDs used when allowlist enforcement is enabled.",
  },
];

export const CONTROL_RECEIPT_SECTIONS: Array<{
  id: ControlReceiptSection;
  label: string;
  description: string;
}> = [
  {
    id: "summary",
    label: "Summary",
    description: "Audit packet status, ledger event context, costs, IDs, and scopes.",
  },
  {
    id: "trace",
    label: "Trace",
    description: "Kernel trace scenarios, invariants, replay checks, alerts, and blocked attempts.",
  },
  {
    id: "payload",
    label: "Payload",
    description: "Args, result preview, and event payloads captured in the receipt.",
  },
  {
    id: "files",
    label: "Files",
    description: "Workspace file operations recorded by this runtime event.",
  },
  {
    id: "metadata",
    label: "Metadata",
    description: "Raw costs, IDs, scopes, and metadata for audit/debug work.",
  },
];

// ---------------------------------------------------------------------------
// Derived view-model types
// ---------------------------------------------------------------------------

export type TimelineSummary = {
  costCents: number;
  tokens: number;
  files: number;
  failures: number;
  latest: ControlTimelineItem | null;
};

export type ControlFailureReviewQueue = {
  totalFailures: number;
  loadedFailures: number;
  missingFailures: number;
  failures: ControlTimelineItem[];
};

export type ControlPostureView = "state" | "timeline" | "policy";

export type ControlPostureAction =
  | {
      kind: "link";
      label: string;
      detail: string;
      href: string;
      action: string;
      variant: "primary" | "secondary";
    }
  | {
      kind: "button";
      label: string;
      detail: string;
      action: string;
      variant: "primary" | "secondary";
      disabled: boolean;
      onClick: () => void;
    };

// ---------------------------------------------------------------------------
// Section normalizers
// ---------------------------------------------------------------------------

export function normalizeControlPolicySection(
  value: string | null | undefined,
): ControlPolicySection {
  return CONTROL_POLICY_SECTIONS.some((section) => section.id === value)
    ? (value as ControlPolicySection)
    : "overview";
}

export function normalizeControlReceiptSection(
  value: string | null | undefined,
): ControlReceiptSection {
  return CONTROL_RECEIPT_SECTIONS.some((section) => section.id === value)
    ? (value as ControlReceiptSection)
    : "summary";
}

export function normalizeControlStatusFilter(
  value: string | null | undefined,
): ControlStatusFilter {
  return value === "failed" ? "failed" : "all";
}

// ---------------------------------------------------------------------------
// Pure timeline transforms (unit-tested via ControlRoom re-exports)
// ---------------------------------------------------------------------------

export function summarizeTimeline(items: ControlTimelineItem[]): TimelineSummary {
  return items.reduce(
    (summary, item) => {
      summary.costCents += item.cost_cents;
      summary.tokens += item.token_count || 0;
      summary.files += item.file_ops.length;
      if (isFailureStatus(item.status)) summary.failures += 1;
      if (
        !summary.latest ||
        Date.parse(item.created_at) > Date.parse(summary.latest.created_at)
      ) {
        summary.latest = item;
      }
      return summary;
    },
    {
      costCents: 0,
      tokens: 0,
      files: 0,
      failures: 0,
      latest: null as ControlTimelineItem | null,
    },
  );
}

export function filterControlTimeline(
  items: ControlTimelineItem[],
  source: string,
  status: ControlStatusFilter,
) {
  const sourceFiltered = source === "all"
    ? items
    : items.filter((item) => item.source === source);
  return status === "failed"
    ? sourceFiltered.filter((item) => isFailureStatus(item.status))
    : sourceFiltered;
}

export function buildControlFailureReviewQueue(
  items: ControlTimelineItem[],
  totalFailures: number,
  limit = 4,
): ControlFailureReviewQueue {
  const failures = sortTimeline(items.filter((item) => isFailureStatus(item.status)));
  const reportedFailures = Math.max(0, totalFailures, failures.length);
  return {
    totalFailures: reportedFailures,
    loadedFailures: failures.length,
    missingFailures: Math.max(0, totalFailures - failures.length),
    failures: failures.slice(0, limit),
  };
}

function isFailureStatus(status: string) {
  return ["denied", "error", "failed"].includes(status.toLowerCase());
}

export function isLiveDagRun(item: ControlTimelineItem) {
  return item.source === "dag" && isLiveStateStatus(item.status);
}

export function sortTimeline(items: ControlTimelineItem[]) {
  return [...items].sort(
    (a, b) => Date.parse(b.created_at) - Date.parse(a.created_at),
  );
}

// ---------------------------------------------------------------------------
// Formatting / labels / routes
// ---------------------------------------------------------------------------

export function controlReceiptRoute(
  receiptPath: string,
  section: ControlReceiptSection = "summary",
) {
  return runtimeControlReceiptHref(receiptPath, section);
}

export function controlPolicyRoute(section: ControlPolicySection = "overview") {
  return section === "overview" ? "/runtime/policy" : `/runtime/policy/${section}`;
}

export function decodeControlReceiptToken(token: string | undefined) {
  if (!token) return null;
  try {
    const normalized = token.replace(/-/g, "+").replace(/_/g, "/");
    const padding = normalized.length % 4 === 0
      ? ""
      : "=".repeat(4 - (normalized.length % 4));
    const binary = atob(normalized + padding);
    const bytes = Uint8Array.from(binary, (char) => char.charCodeAt(0));
    return new TextDecoder().decode(bytes);
  } catch {
    try {
      return decodeURIComponent(token);
    } catch {
      return token;
    }
  }
}

export function money(cents: number) {
  return `$${(cents / 100).toFixed(2)}`;
}

export function moneyFromValue(value: unknown) {
  const amount = Number(value || 0);
  return `$${amount.toFixed(4)}`;
}

export function compact(value: number) {
  return new Intl.NumberFormat(undefined, { notation: "compact" }).format(value);
}

export function fmtDate(value: string) {
  return new Date(value).toLocaleString();
}

export function sourceLabel(source: string) {
  return SOURCE_OPTIONS.find((option) => option.value === source)?.label || statusLabel(source);
}

export function controlTimelineScopeName(sourceName: string, status: ControlStatusFilter) {
  if (status === "failed") {
    return sourceName === "All sources"
      ? "Failed receipts"
      : `Failed ${sourceName.toLowerCase()}`;
  }
  return sourceName;
}

export function sourceBadgeLabel(source: string) {
  switch (source) {
    case "dag":
      return "DAG";
    case "llm":
    case "litellm_reconciled":
    case "litellm_subagent_reconciled":
    case "control_plane_chat":
    case "control_plane_chat_user_llm":
      return "LLM";
    case "subagent":
      return "Subagent";
    case "scope":
      return "Scope";
    case "proof":
      return "Proof";
    case "trial":
      return "Trial";
    case "deployment":
      return "Deployment";
    case "review_loop":
      return "Review loop";
    case "protocol_simulation":
      return "Protocol sim";
    default:
      return statusLabel(source);
  }
}

export function statusLabel(status: string) {
  return status
    .split(/[_\s-]+/)
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

export function budgetPercent(summary: { monthly_budget_cents: number; monthly_spend_cents: number }) {
  if (summary.monthly_budget_cents <= 0) return 0;
  return Math.min(
    100,
    Math.round((summary.monthly_spend_cents / summary.monthly_budget_cents) * 100),
  );
}

export function timelineChips(item: ControlTimelineItem): { label: string; value: string }[] {
  const metadata = item.metadata || {};
  const candidates: [string, unknown][] = [
    ["agent", item.agent_name],
    ["skill", item.skill_name],
    ["trigger", metadata.trigger],
    ["thread", metadata.thread_id],
    ["grant", metadata.grant_id],
    ["head", metadata.head_sha],
    ["room", metadata.room],
    ["score", metadata.score],
    ["protocol", metadata.protocol_id],
    ["template", metadata.template_ref],
    ["episodes", metadata.max_episodes],
  ];
  return candidates.reduce<{ label: string; value: string }[]>((acc, [label, value]) => {
    if (acc.length >= 4) return acc;
    const text = chipText(value);
    if (text) acc.push({ label, value: text });
    return acc;
  }, []);
}

function chipText(value: unknown): string | null {
  if (typeof value === "string" && value.trim()) {
    return value.length > 28 ? value.slice(0, 28) : value;
  }
  if (typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  return null;
}

export function centsInputValue(value: number): string {
  const dollars = value / 100;
  return Number.isInteger(dollars) ? String(dollars) : dollars.toFixed(2);
}

// ---------------------------------------------------------------------------
// Runtime command posture (pure) — folded out of the deleted CommandPosture
// monolith. RuntimePage renders ONE DashboardSurfacePosture strip driven by
// these helpers (mandate D), instead of per-view banner panels.
// ---------------------------------------------------------------------------

export function postureStatusPillTone(
  tone: "emerald" | "amber" | "red",
): StatusPillTone {
  if (tone === "red") return "danger";
  if (tone === "amber") return "authority";
  return "live";
}

export function controlPostureTone({
  summary,
  liveDagRuns,
  policy,
  dirty,
  budgetPct,
}: {
  summary: ControlSummary;
  liveDagRuns: ControlTimelineItem[];
  policy: ControlPolicy | null;
  dirty: boolean;
  budgetPct: number;
}): "emerald" | "amber" | "red" {
  if (summary.failures > 0 || budgetPct >= 90) return "red";
  if (dirty || liveDagRuns.length > 0 || budgetPct >= 70) return "amber";
  if (policy?.only_approved_agents && policy.approved_agents.length === 0) return "amber";
  return "emerald";
}

export function controlPostureLabel({
  summary,
  liveDagRuns,
  policy,
  dirty,
  budgetPct,
}: {
  summary: ControlSummary;
  liveDagRuns: ControlTimelineItem[];
  policy: ControlPolicy | null;
  dirty: boolean;
  budgetPct: number;
}) {
  if (summary.failures > 0) return "attention";
  if (budgetPct >= 90) return "budget hot";
  if (dirty) return "draft policy";
  if (policy?.only_approved_agents && policy.approved_agents.length === 0) return "allowlist gap";
  if (liveDagRuns.length > 0) return "live automation";
  if (budgetPct >= 70) return "budget watch";
  return "healthy";
}

export function controlPostureDetail({
  view,
  summary,
  liveDagRuns,
  activity,
  filteredCount,
  sourceName,
  policy,
  dirty,
  budgetPct,
}: {
  view: ControlPostureView;
  summary: ControlSummary;
  liveDagRuns: ControlTimelineItem[];
  activity?: TimelineSummary;
  filteredCount?: number;
  sourceName: string;
  policy: ControlPolicy | null;
  dirty: boolean;
  budgetPct: number;
}) {
  if (summary.failures > 0) return `${summary.failures} issue${summary.failures === 1 ? "" : "s"} in runtime receipts`;
  if (dirty) return "policy changes are not saved";
  if (policy?.only_approved_agents && policy.approved_agents.length === 0) return "allowlist enforcement has no approved agents";
  if (liveDagRuns.length > 0) return `${liveDagRuns.length} live DAG ${liveDagRuns.length === 1 ? "run" : "runs"}`;
  if (budgetPct >= 70) return `${budgetPct}% of monthly runtime cap used`;
  if (activity) {
    return `${filteredCount ?? 0} ${sourceName.toLowerCase()} receipts, ${money(activity.costCents)} spend`;
  }
  return view === "policy" ? "guardrails saved and runtime clear" : "runtime clear";
}

export function controlPostureAction({
  view,
  summary,
  liveDagRuns,
  activity,
  policy,
  dirty,
  busy,
  onSave,
}: {
  view: ControlPostureView;
  summary: ControlSummary;
  liveDagRuns: ControlTimelineItem[];
  activity?: TimelineSummary;
  policy: ControlPolicy | null;
  dirty: boolean;
  busy: boolean;
  onSave?: () => void;
}): ControlPostureAction {
  const budgetPct = budgetPercent(summary);
  const latestActivity = activity?.latest || null;

  if (dirty && onSave) {
    return {
      kind: "button",
      label: "Save runtime policy changes",
      detail: "Control changes are still in draft state. Save them before relying on the current posture.",
      action: busy ? "Saving..." : "Save controls",
      variant: "primary",
      disabled: busy,
      onClick: onSave,
    };
  }
  if (dirty) {
    return {
      kind: "link",
      label: "Review unsaved policy changes",
      detail: "The runtime policy differs from the saved control-plane copy.",
      href: "/runtime/policy",
      action: "Open policy",
      variant: "primary",
    };
  }
  if (policy?.only_approved_agents && policy.approved_agents.length === 0) {
    return {
      kind: "link",
      label: "Fix empty allowlist enforcement",
      detail: "Approved-agent routing is enforced, but no agents are currently allowed through it.",
      href: controlPolicyRoute("allowlist"),
      action: "Edit allowlist",
      variant: "primary",
    };
  }
  if (budgetPct >= 90) {
    return {
      kind: "link",
      label: "Review budget caps",
      detail: `Monthly runtime spend is at ${budgetPct}% of the configured cap.`,
      href: controlPolicyRoute("budgets"),
      action: "Open budgets",
      variant: "primary",
    };
  }
  if (summary.failures > 0) {
    return {
      kind: "link",
      label: "Inspect failed runtime receipts",
      detail: [
        `${summary.failures} runtime ${summary.failures === 1 ? "issue needs" : "issues need"}`,
        "operator review in the ledger.",
      ].join(" "),
      href: "/runtime/timeline?status=failed",
      action: "Open ledger",
      variant: "primary",
    };
  }
  if (liveDagRuns.length > 0) {
    return {
      kind: "link",
      label: "Watch live automation",
      detail: `${liveDagRuns.length} DAG ${liveDagRuns.length === 1 ? "run is" : "runs are"} still in flight.`,
      href: "/runtime/timeline",
      action: "Open timeline",
      variant: "primary",
    };
  }
  if (latestActivity) {
    return {
      kind: "link",
      label: "Audit the latest receipt",
      detail: `${latestActivity.title} is the newest packet in the selected runtime stream.`,
      href: controlReceiptRoute(latestActivity.receipt_path),
      action: "Open receipt",
      variant: "secondary",
    };
  }
  if (view !== "policy") {
    return {
      kind: "link",
      label: "Review runtime guardrails",
      detail: "Policy controls define spend caps, approval gates, network posture, and agent routing.",
      href: "/runtime/policy",
      action: "Open policy",
      variant: "secondary",
    };
  }
  return {
    kind: "link",
    label: "Open the runtime ledger",
    detail: "Ledger receipts show the operational evidence behind this posture.",
    href: "/runtime/timeline",
    action: "Open ledger",
    variant: "secondary",
  };
}
