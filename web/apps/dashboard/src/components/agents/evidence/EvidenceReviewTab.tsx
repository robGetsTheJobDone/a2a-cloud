import type { ReviewLoop } from "../../../api";
import {
  InlineAlert,
  SummaryMetric,
  SurfacePanel,
  ToolbarButton,
} from "../../DashboardChrome";
import { StateBadge, StatusBadge } from "../../StatusPillAdapters";
import { isActiveReviewLoop, numberValue } from "./evidenceShared";

export function EvidenceReviewTab({
  loops,
  busy,
  error,
  quality,
  mutation,
  risk,
  onStart,
  onStop,
}: {
  loops: ReviewLoop[];
  busy: boolean;
  error: string | null;
  quality: Record<string, unknown>;
  mutation: Record<string, unknown>;
  risk: Record<string, unknown>;
  onStart: () => void;
  onStop: (jobId: string) => void;
}) {
  const active = loops.find((loop) => isActiveReviewLoop(loop.job.status));
  const latest = active || loops[0];
  const latestEvents = latest?.events?.slice(-4) || [];
  return (
    <SurfacePanel as="section" className="mt-3 bg-runtime-bg/70 p-3">
      <div className="flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-[10px] uppercase text-ink-faint">
              Adversarial reviewer loop
            </span>
            {latest && <StateBadge status={latest.job.status} size="xs" />}
            {active && <StatusBadge tone="amber">promotion guarded</StatusBadge>}
          </div>
          <div className="mt-2 grid gap-2 sm:grid-cols-4">
            <SummaryMetric
              label="active"
              value={numberValue(quality.active_review_loop_count)}
              size="compact"
            />
            <SummaryMetric
              label="events"
              value={numberValue(mutation.review_loop_event_count)}
              size="compact"
            />
            <SummaryMetric
              label="fix refs"
              value={numberValue(mutation.review_loop_proposed_fix_count)}
              size="compact"
            />
            <SummaryMetric
              label="freezes"
              value={numberValue(risk.promotion_freeze_count)}
              size="compact"
            />
          </div>
          {latest && (
            <div className="mt-2 truncate font-mono text-[11px] text-ink-faint">
              {latest.job.job_id}
            </div>
          )}
        </div>
        <div className="flex shrink-0 gap-2">
          {active ? (
            <ToolbarButton
              onClick={() => onStop(active.job.job_id)}
              disabled={busy}
              variant="danger"
              size="sm"
            >
              {busy ? "Stopping..." : "Stop"}
            </ToolbarButton>
          ) : (
            <ToolbarButton onClick={onStart} disabled={busy} size="sm">
              {busy ? "Starting..." : "Start"}
            </ToolbarButton>
          )}
        </div>
      </div>
      {error && (
        <InlineAlert tone="red" role="alert" className="mt-2 text-xs">
          {error}
        </InlineAlert>
      )}
      {latestEvents.length > 0 && (
        <div className="mt-3 grid gap-1.5">
          {latestEvents.map((event) => (
            <SurfacePanel
              as="div"
              key={event.event_id}
              className="flex flex-wrap items-center justify-between gap-2 bg-runtime-panel/40 px-2 py-1.5 text-[11px]"
            >
              <span className="font-mono text-ink-soft">{event.event_type}</span>
              <span className="text-ink-faint">{event.message || event.status || "-"}</span>
            </SurfacePanel>
          ))}
        </div>
      )}
    </SurfacePanel>
  );
}
