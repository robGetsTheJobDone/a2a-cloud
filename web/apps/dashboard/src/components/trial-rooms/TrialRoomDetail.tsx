import {
  type AgentListing,
  type TrialRoom,
  type TrialRun,
} from "../../api";
import {
  CodeBlock,
  DataTable,
  EmptyState,
  InfoBlock,
  SegmentedButton,
  SegmentedControl,
  SummaryMetric,
  SurfacePanel,
  ToolbarButton,
} from "../DashboardChrome";
import { StateBadge, StatusBadge } from "../StatusPillAdapters";
import {
  DashboardSurfacePosture,
  type SurfacePostureStatus,
} from "../SurfacePosture";
import { TRIAL_ROOM_VIEWS, type TrialRoomViewId } from "../../navigation";
import {
  changedFileCount,
  fmtMoney,
  outputCoverage,
  receiptId,
  summarizeRunComparison,
  type TrialComparison,
} from "../trialRoomUtils";
import { ComparisonCell, TrialWinnerPanel } from "./shared";
import { RunAgentPanel } from "./RunAgentPanel";
import { TrialRunCard } from "./TrialRunDetail";
import {
  trialRoomNextAction,
  trialRoomPostureLabel,
  trialRoomPostureTone,
  type RoomPatch,
  type TrialRunDetailSectionId,
} from "./helpers";

/**
 * Translate a next-action href produced by the (route-shaped) helper into an
 * in-place selection so the decision CTA never triggers a full-page redirect
 * (mandate C). Recognised forms: /trials/<slug>[/runs/<id>[/<section>]][/<view>].
 */
function dispatchNextAction(
  href: string,
  roomSlug: string,
  onViewChange: (view: TrialRoomViewId) => void,
  onOpenRun: (runId: number | string, section: TrialRunDetailSectionId) => void,
) {
  const base = `/trials/${encodeURIComponent(roomSlug)}`;
  const rest = href.startsWith(base) ? href.slice(base.length) : "";
  const segments = rest.split("/").filter(Boolean);
  if (segments[0] === "runs" && segments[1]) {
    const section = (segments[2] as TrialRunDetailSectionId) || "overview";
    onOpenRun(decodeURIComponent(segments[1]), section);
    return;
  }
  const view = (segments[0] as TrialRoomViewId) || "overview";
  onViewChange(view);
}

export function TrialRoomDetail({
  room,
  agents,
  activeView,
  onViewChange,
  onOpenRun,
  onRoomChange,
  onRefreshAgents,
  agentRefreshBusy,
  agentRefreshErr,
  agentRefreshMessage,
}: {
  room: TrialRoom;
  agents: AgentListing[];
  activeView: TrialRoomViewId;
  onViewChange: (view: TrialRoomViewId) => void;
  onOpenRun: (runId: number | string, section: TrialRunDetailSectionId) => void;
  onRoomChange: RoomPatch;
  onRefreshAgents: () => void;
  agentRefreshBusy: boolean;
  agentRefreshErr: string | null;
  agentRefreshMessage: string | null;
}) {
  const sortedRuns = [...room.runs].sort((a, b) => b.score - a.score || b.id - a.id);
  const winner = room.runs.find((run) => run.id === room.selected_run_id) || null;
  const comparison = summarizeRunComparison(sortedRuns);
  const overviewOpen = activeView === "overview";
  const runOpen = activeView === "run";
  const comparisonOpen = activeView === "comparison";
  const receiptsOpen = activeView === "receipts";

  const postureStatus: SurfacePostureStatus = {
    label: room.status,
    tone:
      room.status === "deployed" || room.status === "selected"
        ? "live"
        : room.status === "failed"
          ? "danger"
          : "peer",
  };

  return (
    <div className="min-w-0 space-y-5 px-4 py-4 sm:px-6">
      <DashboardSurfacePosture
        data-onboarding-target="trial-room-header"
        eyebrow={room.slug}
        title={room.title}
        status={postureStatus}
        metrics={[
          { label: "runs", value: room.runs.length },
          { label: "inputs", value: room.input_paths.length },
          { label: "max sec", value: room.max_runtime_seconds },
          { label: "budget", value: fmtMoney(room.max_cost_cents) },
        ]}
      />

      {room.goal && (
        <p className="max-w-4xl whitespace-pre-wrap text-sm leading-relaxed text-ink-soft">
          {room.goal}
        </p>
      )}

      <SegmentedControl
        role="tablist"
        aria-label={`${room.title} views`}
        className="flex gap-1 overflow-x-auto bg-runtime-panel/60"
      >
        {TRIAL_ROOM_VIEWS.map((view) => (
          <SegmentedButton
            key={view.id}
            role="tab"
            aria-selected={activeView === view.id}
            selected={activeView === view.id}
            onClick={() => onViewChange(view.id)}
            className="whitespace-nowrap"
          >
            {view.label}
          </SegmentedButton>
        ))}
      </SegmentedControl>

      <TrialRoomDecisionPanel
        room={room}
        runs={sortedRuns}
        comparison={comparison}
        winner={winner}
        onViewChange={onViewChange}
        onOpenRun={onOpenRun}
      />

      {overviewOpen && (
        <TrialRoomOverview room={room} winner={winner} />
      )}

      {runOpen && (
        <RunAgentPanel
          room={room}
          agents={agents}
          onRoomChange={onRoomChange}
          onRefreshAgents={onRefreshAgents}
          agentRefreshBusy={agentRefreshBusy}
          agentRefreshErr={agentRefreshErr}
          agentRefreshMessage={agentRefreshMessage}
        />
      )}

      {comparisonOpen && (
        <>
          <RunComparisonPanel room={room} runs={sortedRuns} comparison={comparison} />
          {winner && <TrialWinnerPanel winner={winner} />}
        </>
      )}

      {receiptsOpen && (
        <TrialReceiptsPanel
          room={room}
          runs={sortedRuns}
          comparison={comparison}
          onRoomChange={onRoomChange}
          onOpenRun={onOpenRun}
        />
      )}
    </div>
  );
}

function TrialRoomOverview({
  room,
  winner,
}: {
  room: TrialRoom;
  winner: TrialRun | null;
}) {
  return (
    <>
      <div className="grid gap-4 lg:grid-cols-3">
        <InfoBlock title="Input files">
          {room.input_paths.length ? (
            <div className="space-y-1 font-mono text-xs text-ink-soft">
              {room.input_paths.map((path) => (
                <div key={path} className="truncate">
                  {path}
                </div>
              ))}
            </div>
          ) : (
            <span className="text-ink-muted">No files attached.</span>
          )}
        </InfoBlock>
        <InfoBlock title="Acceptance">
          <div className="whitespace-pre-wrap text-xs text-ink-soft">
            {room.acceptance_criteria || "Human review decides."}
          </div>
        </InfoBlock>
        <InfoBlock title="Output schema">
          <CodeBlock className="max-h-36 border-0 bg-transparent p-0 text-xs text-ink-dim">
            {JSON.stringify(room.output_schema || {}, null, 2)}
          </CodeBlock>
        </InfoBlock>
      </div>

      {winner && <TrialWinnerPanel winner={winner} />}
    </>
  );
}

function TrialRoomDecisionPanel({
  room,
  runs,
  comparison,
  winner,
  onViewChange,
  onOpenRun,
}: {
  room: TrialRoom;
  runs: TrialRun[];
  comparison: TrialComparison;
  winner: TrialRun | null;
  onViewChange: (view: TrialRoomViewId) => void;
  onOpenRun: (runId: number | string, section: TrialRunDetailSectionId) => void;
}) {
  const liveRuns = runs.filter((run) => run.status === "running" || run.status === "evaluating").length;
  const failedRuns = runs.filter((run) => run.status === "failed").length;
  const topRun = runs[0] || null;
  const nextAction = trialRoomNextAction({
    room,
    runs,
    comparison,
    winner,
    liveRuns,
    failedRuns,
    topRun,
  });
  const tone = trialRoomPostureTone({
    runs,
    comparison,
    winner,
    liveRuns,
    failedRuns,
  });
  const label = trialRoomPostureLabel({
    runs,
    comparison,
    winner,
    liveRuns,
    failedRuns,
  });
  const summaryItems = [
    `${runs.length.toLocaleString()} ${runs.length === 1 ? "candidate" : "candidates"}`,
    liveRuns > 0 ? `${liveRuns.toLocaleString()} live` : null,
    comparison.passedRuns > 0
      ? `${comparison.passedRuns.toLocaleString()} passed`
      : null,
    failedRuns > 0 ? `${failedRuns.toLocaleString()} failed` : null,
    winner
      ? `${winner.agent_name} selected`
      : runs.length > 0
        ? `best ${comparison.bestScore}/100`
        : `${room.input_paths.length.toLocaleString()} inputs`,
  ].filter((item): item is string => Boolean(item));

  return (
    <SurfacePanel
      as="section"
      data-onboarding-target="trial-room-posture"
      className="border-runtime-mint-line/30 bg-runtime-bg/70 p-4 sm:p-5"
    >
      <div className="flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <StatusBadge tone={tone} dot={tone === "emerald" || tone === "amber"}>
              {label}
            </StatusBadge>
            <span className="font-mono text-[10px] uppercase tracking-wide text-ink-faint">
              Decision posture
            </span>
          </div>
          <h3 className="mt-2 text-base font-semibold text-ink">
            {nextAction.label}
          </h3>
          <p className="mt-1 max-w-3xl text-sm leading-relaxed text-ink-muted">
            {nextAction.detail}
          </p>
          <div className="mt-3 flex min-w-0 flex-wrap gap-x-3 gap-y-1 font-mono text-[11px] text-ink-faint">
            {summaryItems.map((item) => (
              <span key={item} className="max-w-full truncate">
                {item}
              </span>
            ))}
          </div>
        </div>
        <ToolbarButton
          type="button"
          onClick={() =>
            dispatchNextAction(nextAction.href, room.slug, onViewChange, onOpenRun)
          }
          variant="primary"
          size="md"
          className="w-full justify-center sm:w-auto"
        >
          {nextAction.action}
        </ToolbarButton>
      </div>
    </SurfacePanel>
  );
}

function TrialReceiptsPanel({
  room,
  runs,
  comparison,
  onRoomChange,
  onOpenRun,
}: {
  room: TrialRoom;
  runs: TrialRun[];
  comparison: TrialComparison;
  onRoomChange: RoomPatch;
  onOpenRun: (runId: number | string, section: TrialRunDetailSectionId) => void;
}) {
  return (
    <section>
      <div className="mb-3 flex items-center justify-between">
        <div>
          <div className="text-xs uppercase text-ink-muted">
            Receipts
          </div>
          <h3 className="mt-1 text-lg font-semibold text-ink">
            Candidate runs
          </h3>
        </div>
      </div>
      {runs.length === 0 ? (
        <EmptyState
          title="No receipts yet"
          description="Run a candidate agent to produce the first receipt."
          size="compact"
        />
      ) : (
        <div className="grid gap-3">
          {runs.map((run, index) => (
            <TrialRunCard
              key={run.id}
              room={room}
              run={run}
              rank={index + 1}
              topScore={comparison.bestScore}
              selected={run.id === room.selected_run_id}
              onRoomChange={onRoomChange}
              onOpenRun={onOpenRun}
            />
          ))}
        </div>
      )}
    </section>
  );
}

function RunComparisonPanel({
  room,
  runs,
  comparison,
}: {
  room: TrialRoom;
  runs: TrialRun[];
  comparison: TrialComparison;
}) {
  if (runs.length === 0) return null;
  const selected = runs.find((run) => run.id === room.selected_run_id) || null;

  return (
    <SurfacePanel as="section" className="min-w-0 bg-runtime-bg p-4">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <div className="text-xs uppercase text-ink-muted">
            Comparison
          </div>
          <h3 className="mt-1 text-lg font-semibold text-ink">
            Run evidence
          </h3>
        </div>
        <div className="grid w-full grid-cols-2 gap-2 text-xs sm:w-auto sm:min-w-[360px] sm:grid-cols-4">
          <SummaryMetric label="best" value={comparison.bestScore} />
          <SummaryMetric label="spread" value={comparison.scoreSpread} />
          <SummaryMetric label="passed" value={comparison.passedRuns} />
          <SummaryMetric label="artifacts" value={comparison.artifacts} />
        </div>
      </div>

      <DataTable className="mt-4">
        <div className="hidden grid-cols-[minmax(0,2fr)_80px_90px_90px_minmax(0,1fr)] gap-3 border-b border-runtime-line-soft/60 bg-runtime-panel/50 px-3 py-2 text-[10px] uppercase text-ink-faint md:grid">
          <div>Candidate</div>
          <div>Score</div>
          <div>Status</div>
          <div>Files</div>
          <div>Schema</div>
        </div>
        {runs.map((run) => {
          const coverage = outputCoverage(room, run);
          const isSelected = selected?.id === run.id;
          const coverageText =
            coverage.required.length === 0
              ? "no required keys"
              : coverage.missing.length === 0
              ? "required keys present"
              : `missing ${coverage.missing.join(", ")}`;
          return (
            <div
              key={run.id}
              className={
                "border-b border-runtime-line-soft/60 p-3 text-xs last:border-b-0 md:grid md:grid-cols-[minmax(0,2fr)_80px_90px_90px_minmax(0,1fr)] md:gap-3 md:px-3 md:py-2 " +
                (isSelected ? "bg-signal-live/12" : "bg-runtime-bg")
              }
            >
              <div className="min-w-0">
                <div className="truncate font-mono text-ink">
                  {run.agent_name}.{run.skill_name}
                </div>
                <div className="mt-0.5 truncate text-[11px] text-ink-faint">
                  {receiptId(run).slice(0, 16) || "no receipt yet"}
                </div>
              </div>
              <div className="mt-3 grid gap-2 sm:grid-cols-2 md:contents">
                <ComparisonCell label="Score">
                  <span className="font-mono text-ink">{run.score}</span>
                </ComparisonCell>
                <ComparisonCell label="Status">
                  <StateBadge
                    status={run.status}
                    live={run.status === "running" || run.status === "evaluating"}
                  />
                </ComparisonCell>
                <ComparisonCell label="Files">
                  <span className="text-ink-dim">{changedFileCount(run)}</span>
                </ComparisonCell>
                <ComparisonCell label="Schema">
                  <span
                    className={
                      "break-words " +
                      (coverage.missing.length
                        ? "text-signal-authority"
                        : "text-ink-dim")
                    }
                  >
                    {coverageText}
                  </span>
                </ComparisonCell>
              </div>
            </div>
          );
        })}
      </DataTable>
    </SurfacePanel>
  );
}
