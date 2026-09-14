import {
  CodeBlock,
  EmptyState,
  InlineAlert,
  SegmentedButton,
  SegmentedControl,
  SurfacePanel,
  SummaryMetric,
  ToolbarButton,
} from "../DashboardChrome";
import { StateBadge, StatusBadge } from "../StatusPillAdapters";
import { parseLiveInvocations, type LiveInvocationRecordView } from "../../liveKernelInvocations";
import { KernelTraceCard } from "../KernelTraceCard";
import type { KernelEvolutionRun, ProtocolSimulation } from "../../api";
import { latestTrace } from "./useSimulationsData";
import {
  SIMULATION_RUN_SECTIONS,
  evolutionMutation,
  formatSimulationDate,
  numberValue,
  objectValue,
  textValue,
  type SimulationRunSectionId,
} from "./simulationModel";

export function SimulationRunDetail({
  run,
  activeSection,
  onSelectSection,
  liveInvocations,
  trace,
  replayResult,
  replayBusy,
  onReplay,
}: {
  run: ProtocolSimulation;
  activeSection: SimulationRunSectionId;
  onSelectSection: (section: SimulationRunSectionId) => void;
  liveInvocations: ReturnType<typeof parseLiveInvocations>;
  trace: ReturnType<typeof latestTrace>;
  replayResult: string | null;
  replayBusy: boolean;
  onReplay: () => void;
}) {
  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between gap-3">
        <div>
          <h2 className="text-sm font-semibold text-ink">Run evidence</h2>
          <p className="mt-1 truncate font-mono text-[11px] text-ink-faint">
            {run.job.job_id}
          </p>
        </div>
        <ToolbarButton type="button" onClick={onReplay} disabled={replayBusy}>
          {replayBusy ? "replaying..." : "replay"}
        </ToolbarButton>
      </div>

      <SimulationRunDetailNav
        activeSection={activeSection}
        onSelectSection={onSelectSection}
      />

      {replayResult && (
        <InlineAlert tone="neutral" role="status">
          {replayResult}
        </InlineAlert>
      )}

      {activeSection === "live" ? (
        <SimulationRunLivePanel liveInvocations={liveInvocations} />
      ) : activeSection === "trace" ? (
        <SimulationRunTracePanel trace={trace} />
      ) : (
        <SimulationRunOverviewPanel run={run} />
      )}
    </div>
  );
}

// State-driven section tabs (mandate B/C): selecting a section mutates hoisted
// state via onSelectSection instead of routing, so the detail sheet stays open
// and the lab never unmounts.
function SimulationRunDetailNav({
  activeSection,
  onSelectSection,
}: {
  activeSection: SimulationRunSectionId;
  onSelectSection: (section: SimulationRunSectionId) => void;
}) {
  return (
    <SegmentedControl
      role="tablist"
      aria-label="Simulation run evidence sections"
      className="flex gap-1 overflow-x-auto bg-runtime-panel/60"
    >
      {SIMULATION_RUN_SECTIONS.map((section) => (
        <SegmentedButton
          key={section.id}
          role="tab"
          selected={activeSection === section.id}
          onClick={() => onSelectSection(section.id)}
          className="whitespace-nowrap"
        >
          {section.label}
        </SegmentedButton>
      ))}
    </SegmentedControl>
  );
}

function SimulationRunOverviewPanel({ run }: { run: ProtocolSimulation }) {
  return (
    <SurfacePanel as="section" className="bg-runtime-panel/60 px-3 py-2">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="text-xs font-semibold text-ink">overview</div>
        <StateBadge status={run.job.status} size="xs" />
      </div>
      <div className="mt-3 grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
        <SummaryMetric label="kind" value={run.job.kind || "-"} size="compact" />
        <SummaryMetric label="agent" value={run.job.agent_name || "-"} size="compact" />
        <SummaryMetric label="skill" value={run.job.skill_name || "-"} size="compact" />
        <SummaryMetric
          label="events"
          value={run.events.length}
          size="compact"
        />
        <SummaryMetric
          label="created"
          value={formatSimulationDate(run.job.created_at)}
          size="compact"
          mono={false}
        />
        <SummaryMetric
          label="started"
          value={formatSimulationDate(run.job.started_at)}
          size="compact"
          mono={false}
        />
        <SummaryMetric
          label="completed"
          value={formatSimulationDate(run.job.completed_at)}
          size="compact"
          mono={false}
        />
        <SummaryMetric label="job id" value={run.job.job_id} size="compact" />
      </div>
      {run.job.summary && (
        <p className="mt-3 text-sm leading-relaxed text-ink-dim">
          {run.job.summary}
        </p>
      )}
      {run.job.error && (
        <InlineAlert tone="red" role="alert" className="mt-3 text-xs">
          {run.job.error}
        </InlineAlert>
      )}
    </SurfacePanel>
  );
}

function SimulationRunLivePanel({
  liveInvocations,
}: {
  liveInvocations: ReturnType<typeof parseLiveInvocations>;
}) {
  if (!liveInvocations) {
    return <EmptyState size="compact" title="No live-agent calls" />;
  }

  return (
    <SurfacePanel as="section" className="bg-runtime-panel/60 px-3 py-2">
      <div className="flex items-center justify-between gap-3">
        <span className="text-xs font-semibold text-ink">
          live agents
        </span>
        <StateBadge
          status={liveInvocations.passed ? "passed" : "failed"}
          size="xs"
        />
      </div>
      <div className="mt-1 grid grid-cols-2 gap-2 text-xs text-ink-dim">
        <span>{String(liveInvocations.count || 0)} calls</span>
        <span>{String(liveInvocations.failCount || 0)} failures</span>
      </div>
      {Boolean(liveInvocations.digest) && (
        <div className="mt-1 truncate font-mono text-[11px] text-ink-muted">
          {String(liveInvocations.digest)}
        </div>
      )}
      {liveInvocations.records.length > 0 ? (
        <div className="mt-3 space-y-2">
          {liveInvocations.records.map((record) => (
            <LiveInvocationRecord
              key={`${record.nodeId}:${record.skillName}`}
              record={record}
            />
          ))}
        </div>
      ) : (
        <EmptyState size="compact" title="No invocation records" className="mt-3" />
      )}
    </SurfacePanel>
  );
}

function SimulationRunTracePanel({
  trace,
}: {
  trace: ReturnType<typeof latestTrace>;
}) {
  return trace ? (
    <KernelTraceCard trace={trace} compact />
  ) : (
    <EmptyState size="compact" title="No trace payload" />
  );
}

export function SimulationEvolutionDetail({
  selectedEvolutionRun,
  selectedEvolutionRunId,
  evolutionResult,
  evolutionVariants,
  proposal,
  evolutionReplayResult,
  evolutionReplayBusy,
  directEvolutionRunLoadingId,
  onReplay,
}: {
  selectedEvolutionRun: KernelEvolutionRun | null;
  selectedEvolutionRunId: string;
  evolutionResult: Record<string, unknown> | null;
  evolutionVariants: Record<string, unknown>[];
  proposal: Record<string, unknown> | null;
  evolutionReplayResult: string | null;
  evolutionReplayBusy: boolean;
  directEvolutionRunLoadingId: string;
  onReplay: () => void;
}) {
  return (
    <>
      <div className="mb-3 flex items-center justify-between gap-3">
        <div>
          <h2 className="text-sm font-semibold text-ink">Evolution inspection</h2>
          {selectedEvolutionRun && (
            <p className="mt-1 truncate font-mono text-[11px] text-ink-faint">
              {selectedEvolutionRun.job.job_id}
            </p>
          )}
        </div>
        <ToolbarButton
          type="button"
          onClick={onReplay}
          disabled={!selectedEvolutionRun || evolutionReplayBusy}
        >
          {evolutionReplayBusy ? "replaying..." : "replay"}
        </ToolbarButton>
      </div>
      {selectedEvolutionRun && evolutionResult ? (
        <div className="space-y-3">
          <div className="grid grid-cols-3 gap-2">
            <SummaryMetric
              label="variants"
              value={String(numberValue(evolutionResult.variant_count))}
            />
            <SummaryMetric
              label="winner"
              value={textValue(evolutionResult.winner_variant_id, "-")}
            />
            <SummaryMetric
              label="mode"
              value={evolutionResult.active_apply_enabled === false ? "disabled" : "blocked"}
            />
          </div>
          {evolutionReplayResult && (
            <InlineAlert tone="neutral" role="status">
              {evolutionReplayResult}
            </InlineAlert>
          )}
          <div className="space-y-2">
            {evolutionVariants.map((variant) => {
              const mutation = evolutionMutation(variant);
              const topology = textValue(mutation.topology, textValue(mutation.type, "variant"));
              const passed = Boolean(variant.passed);
              return (
                <div
                  key={String(variant.variant_id)}
                  className={
                    "rounded-md border bg-runtime-panel/50 px-3 py-2 " +
                    (passed ? "border-signal-live/45" : "border-signal-danger/50")
                  }
                >
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0">
                      <div className="truncate text-xs font-semibold text-ink">
                        {String(variant.variant_id)}
                      </div>
                      <div className="mt-0.5 text-[11px] text-ink-muted">
                        {topology} · delta {String(mutation.score_delta ?? 0)}
                      </div>
                    </div>
                    <div className="shrink-0 text-right">
                      <div className={`text-xs font-semibold ${passed ? "text-signal-live" : "text-signal-danger"}`}>
                        {passed ? "passed" : "failed"}
                      </div>
                      <div className="mt-0.5 font-mono text-[11px] text-ink-muted">
                        {String(numberValue(variant.score))}
                      </div>
                    </div>
                  </div>
                  {Boolean(variant.error) && (
                    <InlineAlert
                      tone="red"
                      role="alert"
                      className="mt-2 px-2 py-1 font-mono text-[11px] leading-4"
                    >
                      {String(variant.error)}
                    </InlineAlert>
                  )}
                  {Boolean(variant.proposal_ref) && (
                    <div className="mt-2 truncate font-mono text-[11px] text-ink-muted">
                      {String(variant.proposal_ref)}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
          {proposal && (
            <SurfacePanel as="div" className="border-signal-live/45 bg-signal-live/12 px-3 py-2">
              <div className="flex items-center justify-between gap-3">
                <span className="text-xs font-semibold text-signal-live">disabled proposal</span>
                <StatusBadge tone="emerald" className="rounded-md">
                  {String(objectValue(proposal.draft)?.status || "disabled")}
                </StatusBadge>
              </div>
              <div className="mt-2 truncate font-mono text-[11px] text-signal-live/70">
                {String(proposal.proposal_digest || proposal.proposal_id || "")}
              </div>
            </SurfacePanel>
          )}
        </div>
      ) : directEvolutionRunLoadingId === selectedEvolutionRunId ? (
        <EmptyState
          size="compact"
          title="Loading evolution result"
          description="Fetching owner-scoped variant evidence for this direct result link."
        />
      ) : selectedEvolutionRunId ? (
        <EmptyState
          size="compact"
          title="Evolution result unavailable"
          description="This result could not be loaded from your accessible evolution history."
        />
      ) : (
        <EmptyState
          size="compact"
          title="Open an evolution result"
          description="Choose an evolution run to inspect variants, winner, replay status, and proposal metadata."
        />
      )}
    </>
  );
}

function recordBorderClass(record: LiveInvocationRecordView) {
  if (record.status === "passed") return "border-signal-live/45";
  if (record.timeoutLikely) return "border-signal-authority/45";
  return "border-signal-danger/50";
}

function LiveInvocationRecord({ record }: { record: LiveInvocationRecordView }) {
  const problem = record.error || record.resultError;
  return (
    <div className={`rounded-md border bg-runtime-bg/70 px-3 py-2 ${recordBorderClass(record)}`}>
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="truncate text-xs font-semibold text-ink">{record.agentName}</div>
          <div className="mt-0.5 truncate text-[11px] text-ink-muted">
            {record.skillName}
            {record.role ? ` - ${record.role}` : ""}
          </div>
        </div>
        <div className="shrink-0 text-right">
          <StateBadge status={record.status} size="xs" />
          <div className="mt-0.5 font-mono text-[11px] text-ink-muted">{record.elapsedLabel}</div>
        </div>
      </div>
      {record.timeoutLikely && (
        <InlineAlert tone="amber" role="status" className="mt-2 px-2 py-1 text-[11px] font-medium">
          timeout likely
        </InlineAlert>
      )}
      {problem && (
        <InlineAlert
          tone="red"
          role="alert"
          className="mt-2 px-2 py-1 font-mono text-[11px] leading-4"
        >
          {problem}
        </InlineAlert>
      )}
      {record.resultPreview && (
        <details className="mt-2">
          <summary className="cursor-pointer text-[11px] font-semibold text-ink-dim">
            result preview
          </summary>
          <CodeBlock className="mt-1 max-h-36 border-0 p-2 text-[11px] leading-4 text-ink-soft">
            {record.resultPreview}
          </CodeBlock>
        </details>
      )}
      {record.argsPreview && (
        <details className="mt-2">
          <summary className="cursor-pointer text-[11px] font-semibold text-ink-muted">
            arguments
          </summary>
          <CodeBlock className="mt-1 max-h-28 border-0 p-2 text-[11px] leading-4 text-ink-dim">
            {record.argsPreview}
          </CodeBlock>
        </details>
      )}
    </div>
  );
}
