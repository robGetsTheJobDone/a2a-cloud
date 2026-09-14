import { useCallback, useEffect, useState } from "react";
import {
  listAgentCallLogs,
  type AgentCallLog,
  type AgentProofRun,
  type MyAgentListing,
  type SubagentRun,
} from "../api";
import { AgentInsightsDashboard } from "./AgentInsightsDashboard";
import { fmtDate, statusColor } from "./agentDisplayUtils";
import {
  EmptyState,
  InlineAlert,
  SelectableSurfaceLink,
  SurfacePanel,
  ToolbarButton,
  ToolbarLink,
} from "./DashboardChrome";
import { RunDetailsPanel, type RunDetails } from "./RunDetailsPanel";

export function AgentInsightsPanel({
  agent,
  runs,
  proofs,
  selectedCallId,
  search = "",
}: {
  agent: MyAgentListing;
  runs: SubagentRun[];
  proofs: AgentProofRun[];
  selectedCallId?: string | null;
  search?: string;
}) {
  const [logs, setLogs] = useState<AgentCallLog[] | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setLogs(await listAgentCallLogs(agent.name));
      setErr(null);
    } catch (ex) {
      setErr(ex instanceof Error ? ex.message : String(ex));
    }
  }, [agent.name]);

  useEffect(() => {
    load();
  }, [load]);

  return (
    <div className="mt-4 border-t border-runtime-line-soft/60 pt-4">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <div className="text-[10px] uppercase tracking-wider text-ink-faint">
            Creator insights
          </div>
          <div className="mt-1 text-sm text-ink-soft">
            {logs ? `${logs.length} platform-mediated calls` : "loading..."}
          </div>
        </div>
        <ToolbarButton
          type="button"
          onClick={load}
        >
          Refresh
        </ToolbarButton>
      </div>

      <AgentInsightsDashboard
        agentName={agent.name}
        callLogs={logs || []}
        runs={runs}
        proofs={proofs}
        className="mt-4"
      />

      {err && (
        <InlineAlert tone="red" role="alert" className="mt-3 text-xs">
          {err}
        </InlineAlert>
      )}

      <AgentCallLogExplorer
        agentName={agent.name}
        logs={logs}
        selectedCallId={selectedCallId}
        search={search}
      />

      {logs && logs.length === 0 && (
        <SurfacePanel as="div" className="mt-3 bg-runtime-panel/30 p-3 text-xs text-ink-muted">
          No calls recorded yet.
        </SurfacePanel>
      )}
    </div>
  );
}

function AgentCallLogExplorer({
  agentName,
  logs,
  selectedCallId,
  search,
}: {
  agentName: string;
  logs: AgentCallLog[] | null;
  selectedCallId?: string | null;
  search: string;
}) {
  const selectedLog = selectedCallId
    ? (logs || []).find((log) => log.id === selectedCallId) || null
    : null;
  const visibleLogs = (logs || []).slice(0, 12);
  const detailLoading = Boolean(selectedCallId && logs === null);
  const detailError =
    selectedCallId && logs !== null && !selectedLog
      ? "This call log is no longer in the recent creator insights list."
      : null;

  return (
    <div
      className={
        selectedCallId
          ? "mt-3 grid gap-3 xl:grid-cols-[minmax(280px,440px)_minmax(0,1fr)]"
          : "mt-3 grid gap-2"
      }
    >
      <div className="grid content-start gap-2">
        {visibleLogs.map((log) => (
          <AgentCallLogRow
            key={log.id}
            agentName={agentName}
            log={log}
            selected={log.id === selectedCallId}
            compact={Boolean(selectedCallId)}
            search={search}
          />
        ))}
      </div>

      {selectedCallId && (
        <SurfacePanel as="section" className="min-w-0 bg-runtime-bg">
          <div className="flex flex-col gap-3 border-b border-runtime-line-soft/60 p-3 sm:flex-row sm:items-start sm:justify-between">
            <div className="min-w-0">
              <div className="text-[10px] uppercase text-ink-faint">
                Call detail
              </div>
              <h3 className="mt-1 font-mono text-sm text-ink [overflow-wrap:anywhere]">
                {selectedLog
                  ? `${selectedLog.agent_name}.${selectedLog.skill_name}`
                  : selectedCallId}
              </h3>
              <div className="mt-1 text-xs text-ink-muted [overflow-wrap:anywhere]">
                {selectedLog?.summary || selectedLog?.error || selectedCallId}
              </div>
            </div>
            <ToolbarLink href={agentInsightsRoute(agentName, search)}>
              All calls
            </ToolbarLink>
          </div>
          <RunDetailsPanel
            details={selectedLog ? agentLogDetails(selectedLog) : null}
            loading={detailLoading}
            error={detailError}
            className="p-3"
          />
          {detailError && (
            <div className="px-3 pb-3">
              <EmptyState
                title="Call log not found"
                description="Refresh the insights list or open another recent call."
                size="compact"
              />
            </div>
          )}
        </SurfacePanel>
      )}
    </div>
  );
}

function AgentCallLogRow({
  agentName,
  log,
  selected,
  compact,
  search,
}: {
  agentName: string;
  log: AgentCallLog;
  selected: boolean;
  compact: boolean;
  search: string;
}) {
  return (
    <SelectableSurfaceLink
      href={agentCallRoute(agentName, log.id, search)}
      selected={selected}
      className={
        "bg-runtime-panel/30 text-xs hover:border-runtime-line-mid hover:bg-runtime-panel/50 " +
        (compact
          ? "grid gap-2 p-3"
          : "grid gap-2 p-3 sm:grid-cols-[126px_minmax(0,1fr)] md:grid-cols-[126px_minmax(0,1fr)_88px_78px] md:items-center")
      }
    >
      <div className="flex min-w-0 items-center gap-2">
        <SourcePill source={log.source} />
        <span className={`truncate ${statusColor(log.status)}`}>
          {log.status}
        </span>
      </div>
      <div className="min-w-0">
        <div className="truncate font-mono text-ink">
          {log.skill_name}
        </div>
        <div className="mt-1 truncate text-ink-muted">
          {log.summary || log.error || "No summary."}
        </div>
      </div>
      {!compact && (
        <>
          <div className="text-ink-muted">
            {log.elapsed_ms !== null ? fmtMs(log.elapsed_ms) : "-"}
          </div>
          <div className="text-ink-faint">{fmtDate(log.created_at)}</div>
        </>
      )}
    </SelectableSurfaceLink>
  );
}

function agentInsightsRoute(agentName: string, search = "") {
  return `/my-agents/${encodeURIComponent(agentName)}/insights${search}`;
}

function agentCallRoute(agentName: string, callId: string, search = "") {
  return `/my-agents/${encodeURIComponent(agentName)}/calls/${encodeURIComponent(callId)}${search}`;
}

function SourcePill({ source }: { source: string }) {
  const tone =
    source === "handoff"
      ? "border-signal-protocol/45 bg-signal-protocol/15 text-signal-protocol"
      : source === "trial"
        ? "border-signal-protocol/45 bg-signal-protocol/15 text-signal-protocol"
        : "border-signal-live/45 bg-signal-live/12 text-signal-live";
  return (
    <span className={`rounded-full border px-2 py-0.5 text-[11px] ${tone}`}>
      {source}
    </span>
  );
}

export function agentLogDetails(log: AgentCallLog): RunDetails {
  const stats = [
    { label: "source", value: log.source },
    { label: "grant", value: log.grant_id ? log.grant_id.slice(0, 12) : "-" },
    { label: "elapsed", value: log.elapsed_ms !== null ? fmtMs(log.elapsed_ms) : "-" },
  ];
  return {
    kind: "agent-log",
    id: log.id,
    title: `${log.agent_name}.${log.skill_name}`,
    subtitle: log.source,
    status: log.status,
    summary: log.summary,
    error: log.error,
    stats,
    args: log.args_preview,
    result: log.result_preview,
    file_ops: log.file_ops,
    events: log.events.map((payload, idx) => ({
      id: idx,
      type:
        typeof payload.event_type === "string"
          ? payload.event_type
          : typeof payload.type === "string"
            ? payload.type
            : `event-${idx + 1}`,
      payload,
    })),
    receipt: {
      badge: log.badge,
      metadata: log.metadata,
      created_at: log.created_at,
      started_at: log.started_at,
      completed_at: log.completed_at,
    },
  };
}

function fmtMs(ms: number): string {
  if (ms < 1000) return `${ms}ms`;
  return `${Math.round(ms / 100) / 10}s`;
}
