import {
  InlineAlert,
  SummaryMetric,
  SurfacePanel,
} from "../DashboardChrome";
import { StatusBadge } from "../StatusPillAdapters";
import type {
  ControlPolicy,
  ControlRoom as ControlRoomData,
  ControlSummary,
  ControlTimelineItem,
} from "../../api";
import { budgetPercent } from "./controlData";

/**
 * Compact runtime detail panels kept after the CommandPosture monolith was
 * folded into controlData.ts. The page-level posture strip (RuntimePage's
 * DashboardSurfacePosture) now carries the headline status; these remain as
 * in-body audit panels for the policy/overview views.
 */

export function RuntimeStatePanel({
  summary,
  liveDagRuns,
}: {
  summary: ControlSummary;
  liveDagRuns: ControlTimelineItem[];
}) {
  const budgetPct = budgetPercent(summary);
  const runtimeTone = summary.failures > 0 ? "red" : "emerald";
  const runtimeLabel =
    summary.failures > 0
      ? `${summary.failures} issue${summary.failures === 1 ? "" : "s"}`
      : liveDagRuns.length > 0
        ? "live automation"
        : "healthy";

  return (
    <section className="space-y-3" aria-label="Runtime state">
      <div className="flex items-start justify-between gap-3">
        <div>
          <div className="text-xs uppercase text-ink-muted">
            Runtime state
          </div>
          <div className="mt-1 text-xs leading-relaxed text-ink-faint">
            Current operating posture before policy internals.
          </div>
        </div>
        <StatusBadge tone={runtimeTone} dot>
          {runtimeLabel}
        </StatusBadge>
      </div>

      <div className="grid grid-cols-2 gap-2">
        <SummaryMetric
          label="Live DAGs"
          value={liveDagRuns.length}
          tone={liveDagRuns.length > 0 ? "amber" : "neutral"}
          size="compact"
        />
        <SummaryMetric label="DAG runs" value={summary.dag_runs} size="compact" />
        <SummaryMetric
          label="Budget used"
          value={`${budgetPct}%`}
          tone={budgetPct >= 90 ? "red" : budgetPct >= 70 ? "amber" : "neutral"}
          size="compact"
        />
        <SummaryMetric label="Files" value={summary.files_touched} size="compact" />
        <SummaryMetric label="LLM calls" value={summary.llm_calls_month} size="compact" />
        <SummaryMetric
          label="Failures"
          value={summary.failures}
          tone={summary.failures ? "red" : "emerald"}
          size="compact"
        />
      </div>
    </section>
  );
}

export function PolicySnapshot({
  policy,
  summary,
  dirty,
}: {
  policy: ControlPolicy;
  summary: ControlRoomData["summary"];
  dirty: boolean;
}) {
  const active = [
    policy.require_approval_for_file_writes ? "Write approval" : null,
    policy.deny_external_network ? "Network blocked" : null,
    policy.only_approved_agents ? "Allowlist enforced" : null,
    policy.pii_safe_mode ? "PII-safe" : null,
  ].filter(Boolean) as string[];
  const spendPct =
    summary.monthly_budget_cents > 0
      ? Math.min(100, Math.round((summary.monthly_spend_cents / summary.monthly_budget_cents) * 100))
      : 0;
  return (
    <SurfacePanel as="div" className="bg-runtime-bg p-3">
      <div className="flex items-center justify-between gap-3">
        <div>
          <div className="text-xs uppercase text-ink-muted">
            Policy audit
          </div>
          <div className="mt-1 text-xs text-ink-faint">
            {dirty ? "Unsaved changes" : "Saved policy"}
          </div>
        </div>
        <StatusBadge tone={dirty ? "amber" : "emerald"} dot>
          {dirty ? "draft" : "active"}
        </StatusBadge>
      </div>
      <div className="mt-3 grid grid-cols-2 gap-2 text-xs">
        <SummaryMetric label="Budget used" value={`${spendPct}%`} />
        <SummaryMetric label="Allowlist" value={policy.approved_agents.length} />
      </div>
      <div className="mt-3 flex flex-wrap gap-1.5">
        {(active.length ? active : ["No hard gates"]).map((item) => (
          <span
            key={item}
            className="rounded-md border border-runtime-line-soft/60 bg-runtime-panel/70 px-2 py-0.5 text-[11px] text-ink-dim"
          >
            {item}
          </span>
        ))}
      </div>
      {policy.only_approved_agents && policy.approved_agents.length === 0 && (
        <div className="mt-3">
          <InlineAlert tone="amber">
            Allowlist enforcement is active with no approved agents.
          </InlineAlert>
        </div>
      )}
    </SurfacePanel>
  );
}
