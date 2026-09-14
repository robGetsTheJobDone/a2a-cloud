import {
  EmptyState,
  FormField,
  SelectInput,
  SummaryMetric,
  SurfacePanel,
  ToolbarButton,
} from "../DashboardChrome";
import { StateBadge, StatusBadge } from "../StatusPillAdapters";
import type { ControlTimelineItem } from "../../api";
import { SourcePill } from "./parts";
import {
  CONTROL_STATUS_OPTIONS,
  SOURCE_OPTIONS,
  compact,
  fmtDate,
  money,
  normalizeControlStatusFilter,
  statusLabel,
  timelineChips,
  type ControlStatusFilter,
  type TimelineSummary,
} from "./controlData";

export function ControlTimelineView({
  liveDagRuns,
  recentDagRuns,
  filtered,
  activity,
  source,
  status,
  scopeName,
  onSourceChange,
  onStatusChange,
  onReceipt,
}: {
  liveDagRuns: ControlTimelineItem[];
  recentDagRuns: ControlTimelineItem[];
  filtered: ControlTimelineItem[];
  activity: TimelineSummary;
  source: string;
  status: ControlStatusFilter;
  scopeName: string;
  onSourceChange: (source: string) => void;
  onStatusChange: (status: ControlStatusFilter) => void;
  onReceipt: (item: ControlTimelineItem) => void;
}) {
  return (
    <>
      <div className="flex flex-wrap items-end justify-end gap-3 border-b border-runtime-line-soft/60 pb-4">
        <div className="grid w-full gap-2 sm:w-auto sm:grid-cols-2">
          <FormField label="Filter activity">
            <SelectInput
              id="control-source-filter"
              value={source}
              onChange={(event) => onSourceChange(event.target.value)}
              className="min-w-0"
            >
              {SOURCE_OPTIONS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </SelectInput>
          </FormField>
          <FormField label="Review status">
            <SelectInput
              id="control-status-filter"
              value={status}
              onChange={(event) =>
                onStatusChange(normalizeControlStatusFilter(event.target.value))
              }
              className="min-w-0"
            >
              {CONTROL_STATUS_OPTIONS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </SelectInput>
          </FormField>
        </div>
      </div>

      <DagRunsPanel
        liveDagRuns={liveDagRuns}
        recentDagRuns={recentDagRuns}
        onReceipt={onReceipt}
      />

      <ControlActivitySummary
        filteredCount={filtered.length}
        sourceName={scopeName}
        activity={activity}
        onReceipt={onReceipt}
      />

      {filtered.length === 0 ? (
        <div className="mt-5">
          <EmptyState
            title={status === "failed" ? "No failed receipts" : "No events recorded"}
            description={
              status === "failed"
                ? "Denied, failed, or error receipts will appear here when runtime work needs review."
                : [
                    "Runs, LLM calls, proofs, deployments, and trial activity will appear here",
                    "once the control plane receives receipts.",
                  ].join(" ")
            }
          />
        </div>
      ) : (
        <div className="mt-5 space-y-3" role="list" aria-label={`${scopeName} control events`}>
          {filtered.map((item) => (
            <TimelineRow key={item.id} item={item} onReceipt={onReceipt} />
          ))}
        </div>
      )}
    </>
  );
}

export function ControlActivitySummary({
  filteredCount,
  sourceName,
  activity,
  onReceipt,
}: {
  filteredCount: number;
  sourceName: string;
  activity: TimelineSummary;
  onReceipt: (item: ControlTimelineItem) => void;
}) {
  const latestActivity = activity.latest;

  return (
    <section
      aria-labelledby="control-activity-heading"
      className="mt-5 grid gap-3 xl:grid-cols-[minmax(0,1fr)_minmax(280px,360px)]"
    >
      <SurfacePanel as="div" className="bg-runtime-bg/70 p-4">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="text-xs uppercase text-ink-muted">
              Routing and receipts
            </div>
            <h2 id="control-activity-heading" className="mt-1 text-sm font-semibold text-ink">
              {sourceName} receipt stream
            </h2>
          </div>
          <span className="rounded-full border border-runtime-line-soft/60 bg-runtime-panel/70 px-2 py-0.5 text-[11px] text-ink-dim">
            {filteredCount} shown
          </span>
        </div>
        <p className="mt-2 text-sm leading-relaxed text-ink-muted">
          Latest runs, policy-relevant activity, spend, file operations, and proof status from the control plane.
        </p>
        <div className="mt-4 grid grid-cols-2 gap-2 sm:grid-cols-4">
          <SummaryMetric label="Cost" value={money(activity.costCents)} />
          <SummaryMetric label="Tokens" value={compact(activity.tokens)} />
          <SummaryMetric label="Files" value={activity.files} />
          <SummaryMetric
            label="Issues"
            value={activity.failures}
            tone={activity.failures ? "red" : "emerald"}
          />
        </div>
      </SurfacePanel>

      <SurfacePanel
        as="section"
        className="bg-runtime-bg/70 p-4"
        aria-labelledby="latest-receipt-heading"
      >
        <div className="text-xs uppercase text-ink-muted">
          Control receipt
        </div>
        <h2 id="latest-receipt-heading" className="mt-1 text-sm font-semibold text-ink">
          Latest audit packet
        </h2>
        {latestActivity ? (
          <div className="mt-3 space-y-3">
            <div className="min-w-0">
              <div className="break-words text-sm text-ink-soft">
                {latestActivity.title}
              </div>
              <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-ink-muted">
                <SourcePill source={latestActivity.source} />
                <StateBadge
                  status={latestActivity.status}
                  label={statusLabel(latestActivity.status)}
                />
                <time dateTime={latestActivity.created_at}>
                  {fmtDate(latestActivity.created_at)}
                </time>
              </div>
              <div className="mt-2 break-all font-mono text-[11px] text-ink-faint">
                {latestActivity.receipt_path}
              </div>
            </div>
            <ToolbarButton
              type="button"
              onClick={() => onReceipt(latestActivity)}
              className="w-full"
              aria-label={`Open latest receipt for ${latestActivity.title}`}
            >
              Open latest receipt
            </ToolbarButton>
          </div>
        ) : (
          <p className="mt-3 text-sm leading-relaxed text-ink-muted">
            No matching receipt activity is available for this filter.
          </p>
        )}
      </SurfacePanel>
    </section>
  );
}

export function DagRunsPanel({
  liveDagRuns,
  recentDagRuns,
  onReceipt,
}: {
  liveDagRuns: ControlTimelineItem[];
  recentDagRuns: ControlTimelineItem[];
  onReceipt: (item: ControlTimelineItem) => void;
}) {
  const hasLiveRuns = liveDagRuns.length > 0;
  const rows = hasLiveRuns ? liveDagRuns : recentDagRuns;

  return (
    <SurfacePanel
      as="section"
      className="mt-5 bg-runtime-bg/70 p-4"
      aria-labelledby="live-dag-runs-heading"
    >
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0">
          <div className="text-xs uppercase text-ink-muted">
            {hasLiveRuns ? "Live automation" : "DAG history"}
          </div>
          <h3 id="live-dag-runs-heading" className="mt-1 text-sm font-semibold text-ink">
            {hasLiveRuns
              ? `${liveDagRuns.length} DAG run${liveDagRuns.length === 1 ? "" : "s"} in flight`
              : "No live DAGs right now"}
          </h3>
          <p className="mt-1 text-sm leading-relaxed text-ink-muted">
            Active orchestration stays above the general ledger so operators can see running work before diagnostics.
          </p>
        </div>
        <StatusBadge tone={hasLiveRuns ? "amber" : "emerald"} dot={hasLiveRuns}>
          {hasLiveRuns ? "running" : "idle"}
        </StatusBadge>
      </div>

      {rows.length === 0 ? (
        <EmptyState
          title="No DAG runs recorded"
          description="DAG automation appears here once runtime receipts include orchestration events."
          size="compact"
          className="mt-4"
        />
      ) : (
        <div className="mt-4 grid gap-2">
          {rows.map((item) => (
            <button
              key={item.id}
              type="button"
              onClick={() => onReceipt(item)}
              className="grid min-w-0 gap-3 rounded-md border border-runtime-line-soft/60 bg-runtime-panel/35 px-3 py-2 text-left transition hover:border-runtime-line-mid hover:bg-runtime-panel/60 focus:outline-none focus-visible:ring-2 focus-visible:ring-signal-protocol/35 sm:grid-cols-[minmax(0,1fr)_auto]"
            >
              <span className="min-w-0">
                <span className="flex flex-wrap items-center gap-2">
                  <StateBadge status={item.status} label={statusLabel(item.status)} size="xs" />
                  <time className="text-xs text-ink-faint" dateTime={item.created_at}>
                    {fmtDate(item.created_at)}
                  </time>
                </span>
                <span className="mt-1 block truncate text-sm font-medium text-ink">
                  {item.title}
                </span>
                <span className="mt-0.5 block truncate text-xs text-ink-muted">
                  {item.summary || item.agent_name || item.receipt_path}
                </span>
              </span>
              <span className="grid grid-cols-3 gap-1.5 sm:min-w-[180px]">
                <SummaryMetric label="Cost" value={money(item.cost_cents)} size="compact" />
                <SummaryMetric
                  label="Tokens"
                  value={item.token_count ? compact(item.token_count) : "-"}
                  size="compact"
                />
                <SummaryMetric label="Files" value={item.file_ops.length} size="compact" />
              </span>
            </button>
          ))}
        </div>
      )}
    </SurfacePanel>
  );
}

function TimelineRow({
  item,
  onReceipt,
}: {
  item: ControlTimelineItem;
  onReceipt: (item: ControlTimelineItem) => void;
}) {
  const chips = timelineChips(item);
  const summary = item.summary || item.agent_name || item.id;
  return (
    <SurfacePanel
      as="article"
      role="listitem"
      className="overflow-hidden bg-runtime-bg px-4 py-3"
    >
      <div className="grid min-w-0 gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(220px,280px)_auto] lg:items-start">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <SourcePill source={item.source} />
            <StateBadge status={item.status} label={statusLabel(item.status)} />
            <time className="text-xs text-ink-faint" dateTime={item.created_at}>
              {fmtDate(item.created_at)}
            </time>
          </div>
          <h3 className="mt-2 break-words text-sm font-medium text-ink">
            {item.title}
          </h3>
          <p className="mt-1 break-words text-sm leading-relaxed text-ink-muted">
            {summary}
          </p>
          {chips.length > 0 && (
            <div className="mt-2 flex flex-wrap gap-1.5">
              {chips.map((chip) => (
                <span
                  key={chip.label}
                  className="max-w-full rounded-md border border-runtime-line-soft/60 bg-runtime-panel/60 px-1.5 py-0.5 text-[10px] text-ink-muted"
                >
                  {chip.label}: <span className="break-all font-mono text-ink-dim">{chip.value}</span>
                </span>
              ))}
            </div>
          )}
        </div>
        <div className="grid min-w-0 grid-cols-3 gap-2 text-xs sm:min-w-[220px]">
          <SummaryMetric label="Cost" value={money(item.cost_cents)} />
          <SummaryMetric label="Tokens" value={item.token_count ? compact(item.token_count) : "-"} />
          <SummaryMetric label="Files" value={item.file_ops.length} />
        </div>
        <ToolbarButton
          type="button"
          onClick={() => onReceipt(item)}
          className="w-full lg:w-auto"
          aria-label={`Open receipt for ${item.title}`}
        >
          Open receipt
        </ToolbarButton>
      </div>
    </SurfacePanel>
  );
}
