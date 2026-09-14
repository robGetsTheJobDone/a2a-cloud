import { useCallback, useEffect, useMemo, useState } from "react";
import {
  listLlmUsage,
  listDagRuns,
  listSubagentRuns,
  type DagRun,
  type LLMUsage,
  type SubagentFileOp,
  type SubagentRun,
} from "../api";
import {
  EmptyState as DashboardEmptyState,
  InlineAlert,
  SurfacePanel,
  ToolbarButton,
} from "./DashboardChrome";
import { StateBadge } from "./StatusPillAdapters";
import { DetailSheet } from "./ListDetailLayout";
import { RunDetailsPanel, type RunDetails } from "./RunDetailsPanel";
import { dagRunDetails, llmUsageDetails, subagentRunDetails } from "./chatRunDetails";

/** Run-kind discriminator encoded into the shared run id namespace. */
export type ChatActivityRunKind = "subagent" | "dag" | "llm";

export type ChatActivityRailProps = {
  threadId?: string | null;
  limit?: number;
  variant?: "rail" | "inline" | "embedded";
  className?: string;
  /**
   * Open a run as an in-place detail (mandate B/C: no route redirect). When any
   * open handler is supplied the consumer owns presentation; otherwise the rail
   * self-manages and surfaces the run in its own right-side DetailSheet.
   */
  onOpenRun?: (runId: string, kind: ChatActivityRunKind) => void;
  onOpenSubagentRun?: (run: SubagentRun) => void;
  onOpenDagRun?: (run: DagRun) => void;
  onOpenLlmUsage?: (usage: LLMUsage) => void;
};

/** Build the `${kind}:${id}` namespaced id used for run selection. */
function chatActivityRunId(kind: ChatActivityRunKind, id: string | number): string {
  return `${kind}:${id}`;
}

type ActivityItem =
  | {
      kind: "handoff";
      id: string;
      title: string;
      subtitle: string;
      status: string;
      fileOps: SubagentFileOp[];
      updatedAt: string;
      run: SubagentRun;
    }
  | {
      kind: "dag";
      id: string;
      title: string;
      subtitle: string;
      status: string;
      fileOps: SubagentFileOp[];
      updatedAt: string;
      run: DagRun;
    }
  | {
      kind: "llm";
      id: string;
      title: string;
      subtitle: string;
      status: string;
      fileOps: SubagentFileOp[];
      updatedAt: string;
      usage: LLMUsage;
    };

export function ChatActivityRail({
  threadId,
  limit = 6,
  variant = "rail",
  className = "",
  onOpenRun,
  onOpenSubagentRun,
  onOpenDagRun,
  onOpenLlmUsage,
}: ChatActivityRailProps) {
  // When the consumer wires any open handler it owns the detail presentation
  // (e.g. its own panel). Otherwise the rail self-manages selection and renders
  // an in-place right-side DetailSheet — mandate C: no full-page route hop.
  const selfManaged = !(onOpenRun || onOpenSubagentRun || onOpenDagRun || onOpenLlmUsage);
  const [localSelection, setLocalSelection] = useState<{
    kind: ChatActivityRunKind;
    id: string;
  } | null>(null);
  const [subagentRuns, setSubagentRuns] = useState<SubagentRun[]>([]);
  const [dagRuns, setDagRuns] = useState<DagRun[]>([]);
  const [llmUsage, setLlmUsage] = useState<LLMUsage[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);

  const normalizedLimit = clampLimit(limit);
  const threadFilter = threadId || undefined;

  const refresh = useCallback(async () => {
    try {
      const [nextSubagentRuns, nextDagRuns, nextLlmUsage] = await Promise.all([
        listSubagentRuns({ thread_id: threadFilter, limit: normalizedLimit }),
        listDagRuns({ thread_id: threadFilter, limit: normalizedLimit }),
        listLlmUsage({ thread_id: threadFilter, limit: normalizedLimit }),
      ]);
      setSubagentRuns(nextSubagentRuns);
      setDagRuns(nextDagRuns);
      setLlmUsage(nextLlmUsage);
      setErr(null);
    } catch (ex) {
      setErr(ex instanceof Error ? ex.message : String(ex));
    } finally {
      setLoading(false);
    }
  }, [normalizedLimit, threadFilter]);

  useEffect(() => {
    setLoading(true);
    setSubagentRuns([]);
    setDagRuns([]);
    setLlmUsage([]);
    refresh();
  }, [refresh]);

  const items = useMemo(() => {
    return buildActivityItems(subagentRuns, dagRuns, llmUsage)
      .sort((a, b) => Date.parse(b.updatedAt) - Date.parse(a.updatedAt))
      .slice(0, normalizedLimit);
  }, [dagRuns, llmUsage, normalizedLimit, subagentRuns]);

  const hasLiveRuns = items.some((item) => isLiveStatus(item.status));

  useEffect(() => {
    if (!hasLiveRuns) return;
    const timer = window.setInterval(() => {
      if (document.visibilityState === "hidden") return;
      refresh();
    }, 5000);
    const onVisibilityChange = () => {
      if (document.visibilityState === "visible") refresh();
    };
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  }, [hasLiveRuns, refresh]);

  const embedded = variant === "embedded";
  const shellClass = embedded
    ? "w-full"
    : variant === "inline"
      ? "w-full bg-runtime-bg"
      : "w-full bg-runtime-bg lg:max-w-xs";
  const showHeader = !embedded;
  const listClass = variant === "inline" ? "grid gap-2 md:grid-cols-2" : "space-y-2";
  const scrollClass = embedded
    ? "h-full overflow-y-auto p-2.5"
    : "max-h-[min(70vh,34rem)] overflow-y-auto p-2";
  // Every consumer path now has a destination (external handler or the self
  // -managed sheet), so the Open affordance is always shown.
  const showOpenControls = true;

  const openRun = useCallback(
    (kind: ChatActivityRunKind, id: string | number) => {
      if (selfManaged) {
        setLocalSelection({ kind, id: String(id) });
        return;
      }
      onOpenRun?.(chatActivityRunId(kind, id), kind);
    },
    [onOpenRun, selfManaged],
  );

  const selectedDetails = useMemo<RunDetails | null>(() => {
    if (!localSelection) return null;
    if (localSelection.kind === "subagent") {
      const run = subagentRuns.find((item) => item.grant_id === localSelection.id);
      return run ? subagentRunDetails(run) : null;
    }
    if (localSelection.kind === "dag") {
      const run = dagRuns.find((item) => item.dag_run_id === localSelection.id);
      return run ? dagRunDetails(run) : null;
    }
    const usage = llmUsage.find((item) => String(item.id) === localSelection.id);
    return usage ? llmUsageDetails(usage) : null;
  }, [dagRuns, llmUsage, localSelection, subagentRuns]);

  const content = (
    <>
      {showHeader && (
        <div className="flex items-center justify-between gap-3 border-b border-runtime-line-soft/60 px-3 py-2">
          <div className="min-w-0">
            <div className="text-[10px] uppercase text-ink-faint">
              activity
            </div>
            <h2 className="truncate text-sm font-medium text-ink">
              Handoffs and DAGs
            </h2>
          </div>
          <ToolbarButton
            onClick={refresh}
            disabled={loading}
            size="xs"
          >
            Refresh
          </ToolbarButton>
        </div>
      )}

      {err && (
        <InlineAlert
          tone="red"
          role="alert"
          className="rounded-none border-x-0 border-t-0 px-3 py-2 text-xs"
        >
          {err}
        </InlineAlert>
      )}

      <div className={scrollClass}>
        {embedded && (
          <div className="mb-2 flex items-center justify-between">
            <span className="font-mono text-[10px] uppercase text-ink-faint">
              recent runs
            </span>
            <ToolbarButton
              onClick={refresh}
              disabled={loading}
              size="xs"
              className="font-mono uppercase"
            >
              Refresh
            </ToolbarButton>
          </div>
        )}
        {loading && items.length === 0 ? (
          <DashboardEmptyState title="Loading activity..." size="compact" />
        ) : items.length === 0 ? (
          <DashboardEmptyState
            title={threadId ? "No activity for this thread" : "No recent activity"}
            size="compact"
          />
        ) : (
          <div className={embedded ? "space-y-2" : listClass}>
            {items.map((item) => (
              <ActivityCard
                key={`${item.kind}:${item.id}`}
                item={item}
                showOpenControls={showOpenControls}
                onOpenRun={openRun}
                onOpenSubagentRun={onOpenSubagentRun}
                onOpenDagRun={onOpenDagRun}
                onOpenLlmUsage={onOpenLlmUsage}
              />
            ))}
          </div>
        )}
      </div>
    </>
  );

  const detailSheet = selfManaged ? (
    <DetailSheet
      open={Boolean(localSelection)}
      onClose={() => setLocalSelection(null)}
      size="lg"
      title={
        <span className="flex min-w-0 flex-col">
          <span className="font-mono text-[10px] uppercase tracking-[0.12em] text-ink-faint">
            {localSelection ? `${localSelection.kind} detail` : "detail"}
          </span>
          <span className="truncate font-mono text-sm text-ink [overflow-wrap:anywhere]">
            {selectedDetails?.title || localSelection?.id || "Run detail"}
          </span>
        </span>
      }
      description={selectedDetails?.subtitle}
    >
      <RunDetailsPanel
        details={selectedDetails}
        loading={Boolean(localSelection) && !selectedDetails}
        error={
          localSelection && !selectedDetails
            ? "This run is no longer in the recent activity window. Refresh the rail to reload it."
            : null
        }
        view="all"
      />
    </DetailSheet>
  ) : null;

  if (embedded) {
    return (
      <aside
        className={`${shellClass} ${className} flex h-full flex-col`}
        aria-label="Chat activity"
      >
        {content}
        {detailSheet}
      </aside>
    );
  }

  return (
    <SurfacePanel
      as="aside"
      className={`${shellClass} ${className}`}
      aria-label="Chat activity"
    >
      {content}
      {detailSheet}
    </SurfacePanel>
  );
}

function ActivityCard({
  item,
  showOpenControls,
  onOpenRun,
  onOpenSubagentRun,
  onOpenDagRun,
  onOpenLlmUsage,
}: {
  item: ActivityItem;
  showOpenControls: boolean;
  onOpenRun: (kind: ChatActivityRunKind, id: string | number) => void;
  onOpenSubagentRun?: (run: SubagentRun) => void;
  onOpenDagRun?: (run: DagRun) => void;
  onOpenLlmUsage?: (usage: LLMUsage) => void;
}) {
  const fileSummary = summarizeFileOps(item.fileOps);
  const metricA =
    item.kind === "llm"
      ? { label: "tokens", value: compactNumber(item.usage.total_tokens) }
      : { label: "files", value: fileSummary };
  const metricB =
    item.kind === "llm"
      ? { label: "cost", value: formatUsd(item.usage.cost_usd) }
      : { label: "updated", value: formatTime(item.updatedAt) };

  return (
    <SurfacePanel as="article" className="bg-runtime-panel/40 p-3">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="mb-1 flex items-center gap-2">
            <span className="rounded-md bg-runtime-bg px-1.5 py-0.5 text-[10px] uppercase text-ink-muted">
              {item.kind === "handoff" ? "handoff" : item.kind}
            </span>
            <StateBadge status={item.status} label={labelFor(item.status)} size="xs" />
          </div>
          <div className="truncate text-sm font-medium text-ink" title={item.title}>
            {item.title}
          </div>
          <div className="mt-0.5 truncate text-[11px] text-ink-muted" title={item.subtitle}>
            {item.subtitle}
          </div>
        </div>
        {showOpenControls && (
          <ToolbarButton
            size="xs"
            onClick={() => {
              if (item.kind === "handoff") {
                onOpenRun("subagent", item.run.grant_id);
                onOpenSubagentRun?.(item.run);
              } else if (item.kind === "dag") {
                onOpenRun("dag", item.run.dag_run_id);
                onOpenDagRun?.(item.run);
              } else {
                onOpenRun("llm", item.usage.id);
                onOpenLlmUsage?.(item.usage);
              }
            }}
          >
            Open
          </ToolbarButton>
        )}
      </div>

      <dl className="mt-3 grid grid-cols-2 gap-x-4 gap-y-1 border-t border-runtime-line-soft/60 pt-2 text-[11px]">
        {[metricA, metricB].map((metric) => (
          <div key={metric.label} className="min-w-0">
            <dt className="font-medium uppercase text-ink-faint">
              {metric.label}
            </dt>
            <dd className="mt-0.5 truncate font-mono text-ink-soft">
              {metric.value}
            </dd>
          </div>
        ))}
      </dl>

      {item.kind === "llm" && (
        <div className="mt-2 truncate text-[11px] text-ink-faint">
          {item.usage.prompt_tokens} in · {item.usage.completion_tokens} out
        </div>
      )}

      {item.fileOps.length > 0 && (
        <div className="mt-2 space-y-1">
          {item.fileOps.slice(0, 3).map((op, index) => (
            <div
              key={`${op.path}:${op.op}:${index}`}
              className="flex min-w-0 items-center gap-2 text-[11px]"
              title={`${op.op} ${op.path}`}
            >
              <span className={`shrink-0 font-mono ${fileOpTone(op.op)}`}>
                {op.op}
              </span>
              <span className="min-w-0 flex-1 truncate font-mono text-ink-dim">
                {op.path}
              </span>
            </div>
          ))}
          {item.fileOps.length > 3 && (
            <div className="text-[11px] text-ink-faint">
              +{item.fileOps.length - 3} more files
            </div>
          )}
        </div>
      )}
    </SurfacePanel>
  );
}

function buildActivityItems(
  subagentRuns: SubagentRun[],
  dagRuns: DagRun[],
  llmUsage: LLMUsage[],
): ActivityItem[] {
  return [
    ...subagentRuns.map((run): ActivityItem => ({
      kind: "handoff",
      id: run.grant_id,
      title: run.agent_name || "Subagent handoff",
      subtitle: run.skill_name || run.grant_id,
      status: run.status,
      fileOps: run.file_ops || [],
      updatedAt: run.updated_at || run.created_at,
      run,
    })),
    ...dagRuns.map((run): ActivityItem => ({
      kind: "dag",
      id: run.dag_run_id,
      title: run.goal || "Agent DAG",
      subtitle: `${run.nodes.length} ${run.nodes.length === 1 ? "node" : "nodes"}`,
      status: run.status,
      fileOps: dagFileOps(run),
      updatedAt: run.updated_at || run.created_at,
      run,
    })),
    ...llmUsage.map((usage): ActivityItem => ({
      kind: "llm",
      id: String(usage.id),
      title: usage.model || "LLM call",
      subtitle: [
        usage.agent_name && usage.skill_name
          ? `${usage.agent_name}.${usage.skill_name}`
          : llmUsageSourceLabel(usage.source),
        usage.provider,
      ].filter(Boolean).join(" · "),
      status: usage.status,
      fileOps: [],
      updatedAt: usage.created_at,
      usage,
    })),
  ];
}

function llmUsageSourceLabel(source: string): string {
  switch (source) {
    case "litellm_reconciled":
      return "LiteLLM reconciled";
    case "litellm_subagent_reconciled":
      return "Subagent LLM";
    case "control_plane_chat":
    case "control_plane_chat_user_llm":
      return "Chat LLM";
    default:
      return labelFor(source);
  }
}

function dagFileOps(run: DagRun): SubagentFileOp[] {
  const seen = new Set<string>();
  const ops: SubagentFileOp[] = [];
  for (const node of run.nodes) {
    for (const op of node.file_ops || []) {
      const key = `${op.op}:${op.path}`;
      if (seen.has(key)) continue;
      seen.add(key);
      ops.push(op);
    }
  }
  return ops;
}

function summarizeFileOps(ops: SubagentFileOp[]): string {
  if (ops.length === 0) return "0";
  const files = new Set(ops.map((op) => op.path)).size;
  return files === ops.length ? String(files) : `${files}/${ops.length}`;
}

function isLiveStatus(status: string): boolean {
  return ["running", "queued", "waiting", "pending", "planned"].includes(
    status.toLowerCase(),
  );
}

function fileOpTone(op: string): string {
  if (op === "create") return "text-signal-live";
  if (op === "update") return "text-signal-authority";
  if (op === "delete") return "text-signal-danger";
  return "text-ink-muted";
}

function labelFor(value: string): string {
  return value
    .split(/[_\s-]+/)
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

function formatTime(value: string): string {
  const time = Date.parse(value);
  if (!Number.isFinite(time)) return "-";
  const diffMs = Date.now() - time;
  const absMs = Math.abs(diffMs);
  const minute = 60 * 1000;
  const hour = 60 * minute;
  const day = 24 * hour;
  if (absMs < minute) return "now";
  const suffix = diffMs < 0 ? "from now" : "ago";
  if (absMs < hour) return `${Math.round(absMs / minute)}m ${suffix}`;
  if (absMs < day) return `${Math.round(absMs / hour)}h ${suffix}`;
  if (absMs < 7 * day) return `${Math.round(absMs / day)}d ${suffix}`;
  return new Date(time).toLocaleDateString();
}

function compactNumber(value: number): string {
  return new Intl.NumberFormat(undefined, { notation: "compact" }).format(value || 0);
}

function formatUsd(value: number): string {
  if (!value) return "$0";
  if (value < 0.01) return `$${value.toFixed(4)}`;
  return `$${value.toFixed(2)}`;
}

function clampLimit(limit: number): number {
  if (!Number.isFinite(limit)) return 6;
  return Math.min(Math.max(Math.trunc(limit), 1), 100);
}
