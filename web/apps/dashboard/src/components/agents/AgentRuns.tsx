import { useEffect, useMemo, useState } from "react";
import {
  getSubagentRun,
  type SubagentRun,
} from "../../api";
import { subagentRunDetails } from "../chatRunDetails";
import {
  RunDetailsPanel,
  type RunDetails,
} from "../RunDetailsPanel";
import {
  DefinitionRow,
  SegmentedControl,
  SelectableSurfaceLink,
  SurfacePanel,
  TabLink,
  ToolbarLink,
} from "../DashboardChrome";
import { StateBadge } from "../StatusPillAdapters";
import {
  AGENT_RUN_DETAIL_VIEWS,
  type AgentRunDetailView,
  fmtDate,
  myAgentRunRoute,
  myAgentsRoute,
} from "./agentTypes";

export function AgentRunsPanel({
  agentName,
  runs,
  selectedRunId,
  selectedRunView,
  search,
}: {
  agentName: string;
  runs: SubagentRun[];
  selectedRunId?: string | null;
  selectedRunView: AgentRunDetailView;
  search: string;
}) {
  const selectedListRun = useMemo(
    () => runs.find((run) => run.grant_id === selectedRunId) || null,
    [runs, selectedRunId],
  );
  const [details, setDetails] = useState<RunDetails | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    if (!selectedRunId) {
      setDetails(null);
      setErr(null);
      setLoading(false);
      return;
    }

    let cancelled = false;
    setDetails(selectedListRun ? subagentRunDetails(selectedListRun) : null);
    setErr(null);
    setLoading(true);

    void getSubagentRun(selectedRunId)
      .then((run) => {
        if (!cancelled) setDetails(subagentRunDetails(run));
      })
      .catch((ex) => {
        if (!cancelled) setErr(ex instanceof Error ? ex.message : String(ex));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [selectedListRun, selectedRunId]);

  return (
    <div className="mt-4 border-t border-runtime-line-soft/60 pt-4">
      <div className="text-[10px] uppercase text-ink-faint">
        Recent subagent runs
      </div>
      {runs.length === 0 ? (
        <SurfacePanel as="div" className="mt-2 bg-runtime-panel/40 p-3 text-xs text-ink-muted">
          No recent runs recorded for this agent.
        </SurfacePanel>
      ) : (
        <div
          className={
            selectedRunId
              ? "mt-2 grid gap-3 xl:grid-cols-[minmax(280px,420px)_minmax(0,1fr)]"
              : "mt-2 grid gap-2"
          }
        >
          <div className="grid content-start gap-2">
            {runs.map((run) => (
              <AgentRunRow
                key={run.grant_id}
                agentName={agentName}
                run={run}
                selected={run.grant_id === selectedRunId}
                search={search}
                compact={Boolean(selectedRunId)}
              />
            ))}
          </div>

          {selectedRunId && (
            <SurfacePanel as="section" className="min-w-0 bg-runtime-bg">
              <div className="flex flex-col gap-3 border-b border-runtime-line-soft/60 p-3 sm:flex-row sm:items-start sm:justify-between">
                <div className="min-w-0">
                  <div className="text-[10px] uppercase text-ink-faint">
                    Run detail
                  </div>
                  <h3 className="mt-1 font-mono text-sm text-ink [overflow-wrap:anywhere]">
                    {details?.title || selectedRunId}
                  </h3>
                  <div className="mt-1 text-xs text-ink-muted [overflow-wrap:anywhere]">
                    {details?.subtitle || selectedRunId}
                  </div>
                </div>
                <ToolbarLink href={myAgentsRoute(agentName, "runs", search)}>
                  All runs
                </ToolbarLink>
              </div>
              <AgentRunDetailNav
                agentName={agentName}
                grantId={selectedRunId}
                activeView={selectedRunView}
                search={search}
              />
              <RunDetailsPanel
                details={details}
                loading={loading}
                error={err}
                view={selectedRunView}
                className="p-3"
              />
            </SurfacePanel>
          )}
        </div>
      )}
    </div>
  );
}

function AgentRunDetailNav({
  agentName,
  grantId,
  activeView,
  search,
}: {
  agentName: string;
  grantId: string;
  activeView: AgentRunDetailView;
  search: string;
}) {
  const current =
    AGENT_RUN_DETAIL_VIEWS.find((view) => view.id === activeView) ||
    AGENT_RUN_DETAIL_VIEWS[0];
  return (
    <div className="border-b border-runtime-line-soft/60 px-3 py-3">
      <SegmentedControl
        role="tablist"
        aria-label="Run detail views"
        className="flex flex-wrap gap-1 bg-runtime-panel/70"
      >
        {AGENT_RUN_DETAIL_VIEWS.map((view) => (
          <TabLink
            key={view.id}
            href={myAgentRunRoute(agentName, grantId, search, view.id)}
            selected={view.id === activeView}
          >
            {view.label}
          </TabLink>
        ))}
      </SegmentedControl>
      <div className="mt-2 text-xs leading-relaxed text-ink-muted">
        {current.description}
      </div>
    </div>
  );
}

function AgentRunRow({
  agentName,
  run,
  selected,
  search,
  compact,
}: {
  agentName: string;
  run: SubagentRun;
  selected: boolean;
  search: string;
  compact: boolean;
}) {
  return (
    <SelectableSurfaceLink
      href={myAgentRunRoute(agentName, run.grant_id, search)}
      selected={selected}
      className={
        "bg-runtime-panel/40 text-xs " +
        (compact ? "p-3" : "grid gap-3 p-3 lg:grid-cols-[minmax(0,1fr)_96px_96px_92px] lg:items-center")
      }
    >
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          <span className="truncate font-mono text-ink-soft">
            {run.skill_name}
          </span>
          <StateBadge status={run.status} size="xs" />
        </div>
        <div className="mt-1 truncate text-ink-muted">
          {run.summary || `${run.file_ops_count ?? run.file_ops.length} files touched`}
        </div>
        <div className="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-ink-faint">
          <span className="font-mono">{run.grant_id}</span>
          <span>{run.file_ops_count ?? run.file_ops.length} file ops</span>
        </div>
      </div>
      {!compact && (
        <>
          <DefinitionRow label="created" value={fmtDate(run.created_at)} />
          <DefinitionRow label="completed" value={run.completed_at ? fmtDate(run.completed_at) : "-"} />
          <DefinitionRow label="events" value={String(run.events.length)} />
        </>
      )}
    </SelectableSurfaceLink>
  );
}
