/**
 * EvidenceSummary — the "Evidence" sub-panel of the bounty lifecycle surface
 * (mandate D: split the former 1187-LOC BountyLifecyclePanel into focused
 * sub-panels). Renders evidence metrics, linked trial rooms, and the claimed
 * agent's trial runs. Pure presentation over a precomputed lifecycle summary.
 */
import type { Bounty, TrialRoom, TrialRun } from "../api";
import {
  CompactFact,
  SummaryMetric,
  SurfacePanel,
} from "./DashboardChrome";
import { StateBadge } from "./StatusPillAdapters";
import {
  changedFileCount,
  formatDate,
  formatMs,
  receiptId,
  type BountyLifecycleSummary,
} from "./bountyLifecycleShared";

export function EvidenceSummary({
  bounty,
  summary,
}: {
  bounty: Bounty;
  summary: BountyLifecycleSummary;
}) {
  return (
    <section className="mt-4 min-w-0 space-y-4" aria-label="Bounty evidence">
      <EvidenceMetrics summary={summary} />
      <LinkedTrialRooms rooms={summary.linkedTrialRooms} bounty={bounty} />
      <TrialRunList runs={summary.claimedAgentRuns} />
    </section>
  );
}

function EvidenceMetrics({ summary }: { summary: BountyLifecycleSummary }) {
  return (
    <div className="grid min-w-0 gap-2 text-xs sm:grid-cols-2 lg:grid-cols-4">
      <SummaryMetric label="linked trials" value={summary.evidence.linkedTrials} />
      <SummaryMetric label="agent runs" value={summary.evidence.agentRuns} />
      <SummaryMetric
        label="best score"
        value={
          summary.evidence.bestScore === null
            ? "-"
            : `${summary.evidence.bestScore}/100`
        }
        tone={
          summary.evidence.bestScore !== null && summary.evidence.bestScore >= 70
            ? "emerald"
            : "neutral"
        }
      />
      <SummaryMetric
        label="receipts"
        value={summary.evidence.receipts}
        tone={summary.evidence.receipts > 0 ? "emerald" : "neutral"}
      />
    </div>
  );
}

function LinkedTrialRooms({
  rooms,
  bounty,
}: {
  rooms: readonly TrialRoom[];
  bounty: Bounty;
}) {
  return (
    <SurfacePanel as="section" className="min-w-0 p-3">
      <div className="flex min-w-0 items-center justify-between gap-3">
        <h4 className="truncate text-xs font-medium uppercase text-ink-muted">
          Linked trials
        </h4>
        <span className="font-mono text-[11px] text-ink-faint">
          {rooms.length}
        </span>
      </div>
      {rooms.length === 0 ? (
        <p className="mt-2 text-xs leading-relaxed text-ink-muted">
          No trial room references this bounty yet. Use a trial to compare agents
          on the bounty criteria before fulfillment.
        </p>
      ) : (
        <ul className="mt-3 space-y-2" aria-label={`Trial rooms for ${bounty.title}`}>
          {rooms.map((room) => (
            <SurfacePanel
              as="li"
              key={room.slug}
              className="min-w-0 bg-runtime-bg p-3"
            >
              <div className="flex min-w-0 flex-wrap items-center gap-2">
                <span className="min-w-0 flex-1 truncate text-sm text-ink">
                  {room.title}
                </span>
                <SmallStatusPill status={room.status} />
              </div>
              <div className="mt-2 grid gap-2 text-[11px] sm:grid-cols-4">
                <CompactFact label="runs" value={room.runs.length} size="compact" />
                <CompactFact label="inputs" value={room.input_paths.length} size="compact" />
                <CompactFact
                  label="selected"
                  value={room.selected_run_id ? `#${room.selected_run_id}` : "-"}
                  size="compact"
                />
                <CompactFact label="updated" value={formatDate(room.updated_at)} size="compact" />
              </div>
            </SurfacePanel>
          ))}
        </ul>
      )}
    </SurfacePanel>
  );
}

function TrialRunList({ runs }: { runs: readonly TrialRun[] }) {
  return (
    <SurfacePanel as="section" className="min-w-0 p-3">
      <div className="flex min-w-0 items-center justify-between gap-3">
        <h4 className="truncate text-xs font-medium uppercase text-ink-muted">
          Claimed agent trial runs
        </h4>
        <span className="font-mono text-[11px] text-ink-faint">
          {runs.length}
        </span>
      </div>
      {runs.length === 0 ? (
        <p className="mt-2 text-xs leading-relaxed text-ink-muted">
          No run from the claimed agent is attached to linked trials yet.
        </p>
      ) : (
        <ul className="mt-3 space-y-2" aria-label="Claimed agent trial runs">
          {runs.slice(0, 5).map((run) => (
            <SurfacePanel
              as="li"
              key={run.id}
              className="min-w-0 bg-runtime-bg p-3"
            >
              <div className="flex min-w-0 flex-wrap items-center gap-2">
                <span className="min-w-0 flex-1 break-all font-mono text-xs text-ink-soft">
                  {run.agent_name}.{run.skill_name}
                </span>
                <SmallStatusPill status={run.status} />
              </div>
              <div className="mt-2 grid gap-2 text-[11px] sm:grid-cols-4">
                <CompactFact label="score" value={`${run.score}/100`} size="compact" />
                <CompactFact
                  label="receipt"
                  value={receiptId(run) ? receiptId(run).slice(0, 12) : "-"}
                  mono
                  size="compact"
                />
                <CompactFact label="files" value={changedFileCount(run)} size="compact" />
                <CompactFact label="elapsed" value={formatMs(run.elapsed_ms)} size="compact" />
              </div>
              {(run.summary || run.evaluator_notes || run.error) && (
                <p className="mt-2 line-clamp-2 break-words text-xs text-ink-dim">
                  {run.error || run.summary || run.evaluator_notes}
                </p>
              )}
            </SurfacePanel>
          ))}
          {runs.length > 5 && (
            <li className="text-[11px] text-ink-faint">
              +{runs.length - 5} older runs
            </li>
          )}
        </ul>
      )}
    </SurfacePanel>
  );
}

export function SmallStatusPill({ status }: { status: string }) {
  return <StateBadge status={status} size="xs" className="shrink-0" />;
}
