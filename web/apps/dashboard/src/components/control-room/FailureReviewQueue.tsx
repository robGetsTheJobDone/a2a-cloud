import {
  EmptyState,
  InlineAlert,
  SummaryMetric,
  SurfacePanel,
  ToolbarButton,
  ToolbarLink,
} from "../DashboardChrome";
import { StateBadge, StatusBadge } from "../StatusPillAdapters";
import type { ControlTimelineItem } from "../../api";
import { SourcePill } from "./parts";
import { fmtDate, statusLabel, type ControlFailureReviewQueue } from "./controlData";

export function ControlFailureReviewPanel({
  queue,
  onReceipt,
}: {
  queue: ControlFailureReviewQueue;
  onReceipt: (item: ControlTimelineItem) => void;
}) {
  const hasFailures = queue.totalFailures > 0;

  return (
    <SurfacePanel
      as="section"
      className="mt-5 bg-runtime-bg/70 p-4"
      aria-labelledby="failure-review-heading"
    >
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0">
          <div className="text-xs uppercase text-ink-muted">
            Failure review
          </div>
          <h2 id="failure-review-heading" className="mt-1 text-sm font-semibold text-ink">
            {hasFailures ? "Failed receipts ready for review" : "No failed receipts"}
          </h2>
          <p className="mt-1 text-sm leading-relaxed text-ink-muted">
            Denied, failed, and error receipts are pulled out of the mixed ledger so the
            next runtime issue is one action away.
          </p>
        </div>
        <StatusBadge tone={hasFailures ? "red" : "emerald"} dot={hasFailures}>
          {hasFailures ? `${queue.totalFailures} issue${queue.totalFailures === 1 ? "" : "s"}` : "clear"}
        </StatusBadge>
      </div>

      <div className="mt-4 grid grid-cols-3 gap-2">
        <SummaryMetric
          label="Reported"
          value={queue.totalFailures}
          tone={hasFailures ? "red" : "emerald"}
          size="compact"
        />
        <SummaryMetric
          label="Loaded"
          value={queue.loadedFailures}
          tone={queue.loadedFailures > 0 ? "red" : "neutral"}
          size="compact"
        />
        <SummaryMetric
          label="Outside window"
          value={queue.missingFailures}
          tone={queue.missingFailures > 0 ? "amber" : "neutral"}
          size="compact"
        />
      </div>

      {queue.failures.length > 0 ? (
        <div className="mt-4 grid gap-2">
          {queue.failures.map((item) => (
            <div
              key={item.id}
              className={[
                "grid min-w-0 gap-3 rounded-md border border-signal-danger/50 bg-signal-danger/12 px-3 py-2",
                "sm:grid-cols-[minmax(0,1fr)_auto] sm:items-center",
              ].join(" ")}
            >
              <div className="min-w-0">
                <div className="flex flex-wrap items-center gap-2">
                  <SourcePill source={item.source} />
                  <StateBadge status={item.status} label={statusLabel(item.status)} size="xs" />
                  <time className="text-xs text-ink-faint" dateTime={item.created_at}>
                    {fmtDate(item.created_at)}
                  </time>
                </div>
                <div className="mt-1 truncate text-sm font-medium text-ink">
                  {item.title}
                </div>
                <div className="mt-0.5 truncate text-xs text-ink-muted">
                  {item.summary || item.agent_name || item.receipt_path}
                </div>
              </div>
              <ToolbarButton
                type="button"
                onClick={() => onReceipt(item)}
                className="w-full sm:w-auto"
                aria-label={`Open failed receipt for ${item.title}`}
              >
                Open receipt
              </ToolbarButton>
            </div>
          ))}
        </div>
      ) : hasFailures ? (
        <div className="mt-4">
          <InlineAlert tone="amber">
            The runtime summary reports failures, but none are present in the loaded receipt window.
          </InlineAlert>
        </div>
      ) : (
        <EmptyState
          title="Failure queue clear"
          description="Denied, failed, or error receipts will appear here when runtime work needs review."
          size="compact"
          className="mt-4"
        />
      )}

      {queue.missingFailures > 0 && queue.failures.length > 0 && (
        <div className="mt-3">
          <InlineAlert tone="amber">
            {queue.missingFailures} reported failure
            {queue.missingFailures === 1 ? " is" : "s are"} outside the loaded receipt window.
          </InlineAlert>
        </div>
      )}

      <div className="mt-4 flex justify-end">
        <ToolbarLink
          href="/runtime/timeline?status=failed"
          variant={hasFailures ? "primary" : "secondary"}
        >
          Open failed ledger
        </ToolbarLink>
      </div>
    </SurfacePanel>
  );
}
