import { useId, useMemo } from "react";
import type {
  AgentCallLog,
  AgentProofRun,
  SubagentFileOp,
  SubagentRun,
} from "../api";
import {
  EmptyState,
  ProgressBar,
  SectionPanel,
  SummaryMetric,
  SummaryStrip,
  SurfacePanel,
  type StatusBadgeTone,
} from "./DashboardChrome";
import { StateBadge, StatusBadge } from "./StatusPillAdapters";

type AgentInsightOutcome =
  | "success"
  | "failure"
  | "active"
  | "pending"
  | "unknown";

type AgentInsightActivityKind = "call" | "run" | "proof";

type AgentInsightActivity = {
  id: string;
  kind: AgentInsightActivityKind;
  source: string;
  agentName: string;
  skillName: string;
  status: string;
  badge: string | null;
  outcome: AgentInsightOutcome;
  summary: string | null;
  error: string | null;
  elapsedMs: number | null;
  fileOps: readonly SubagentFileOp[];
  fileOpsCount: number;
  createdAt: string;
  startedAt: string | null;
  completedAt: string | null;
};

type AgentInsightSkillStat = {
  skillName: string;
  total: number;
  success: number;
  failure: number;
  averageLatencyMs: number | null;
};

type AgentInsightSourceStat = {
  source: string;
  total: number;
  success: number;
  failure: number;
  percent: number;
};

type AgentInsightFileStat = {
  path: string;
  total: number;
  creates: number;
  updates: number;
  deletes: number;
};

export type AgentInsightsSummary = {
  agentName: string;
  totalActivities: number;
  totalCalls: number;
  totalRuns: number;
  totalProofs: number;
  success: number;
  failure: number;
  active: number;
  pending: number;
  unknown: number;
  successRate: number;
  latencySamples: number;
  averageLatencyMs: number | null;
  medianLatencyMs: number | null;
  p95LatencyMs: number | null;
  fileOpsTotal: number;
  fileCreates: number;
  fileUpdates: number;
  fileDeletes: number;
  uniqueFiles: number;
  activities: AgentInsightActivity[];
  topSkills: AgentInsightSkillStat[];
  sourceMix: AgentInsightSourceStat[];
  topFiles: AgentInsightFileStat[];
};

export type AgentInsightsDashboardProps = {
  agentName: string;
  callLogs: readonly AgentCallLog[];
  runs: readonly SubagentRun[];
  proofs: readonly AgentProofRun[];
  className?: string;
  maxRecentCalls?: number;
  maxTopSkills?: number;
  maxTopFiles?: number;
};

type Tone = "neutral" | "emerald" | "amber" | "red";

const NUMBER_FORMAT = new Intl.NumberFormat(undefined);

export function AgentInsightsDashboard({
  agentName,
  callLogs,
  runs,
  proofs,
  className,
  maxRecentCalls = 8,
  maxTopSkills = 5,
  maxTopFiles = 5,
}: AgentInsightsDashboardProps) {
  const titleId = useId();
  const summary = useMemo(
    () => summarizeAgentInsights(agentName, callLogs, runs, proofs),
    [agentName, callLogs, runs, proofs],
  );
  const recentActivities = summary.activities.slice(0, maxRecentCalls);
  const topSkills = summary.topSkills.slice(0, maxTopSkills);
  const topFiles = summary.topFiles.slice(0, maxTopFiles);
  const classes = ["space-y-4", className || ""].filter(Boolean).join(" ");

  return (
    <section aria-labelledby={titleId} className={classes}>
      <div className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
        <div className="min-w-0">
          <div className="text-[10px] uppercase text-ink-faint">
            Creator insights
          </div>
          <h2
            id={titleId}
            className="mt-1 truncate font-mono text-base font-semibold text-ink"
          >
            {summary.agentName}
          </h2>
        </div>
        <div className="flex flex-wrap gap-2 text-[11px] text-ink-muted">
          <span>{formatCount(summary.totalCalls)} calls</span>
          <span>{formatCount(summary.totalRuns)} runs</span>
          <span>{formatCount(summary.totalProofs)} proofs</span>
        </div>
      </div>

      <SummaryStrip className="xl:grid-cols-5">
        <SummaryMetric
          label="Total"
          value={formatCount(summary.totalActivities)}
          detail={`${formatCount(summary.totalCalls)} logs, ${formatCount(
            summary.totalRuns,
          )} runs`}
        />
        <SummaryMetric
          label="Success"
          value={`${summary.successRate}%`}
          detail={`${formatCount(summary.success)} success / ${formatCount(
            summary.failure,
          )} failed`}
          tone={summary.failure > 0 ? "amber" : "emerald"}
        />
        <SummaryMetric
          label="Failures"
          value={formatCount(summary.failure)}
          detail={`${formatCount(summary.active)} active, ${formatCount(
            summary.pending,
          )} pending`}
          tone={summary.failure > 0 ? "red" : "neutral"}
        />
        <SummaryMetric
          label="Latency"
          value={formatDuration(summary.medianLatencyMs)}
          detail={
            summary.latencySamples > 0
              ? `${formatDuration(summary.averageLatencyMs)} avg, ${formatDuration(
                  summary.p95LatencyMs,
                )} p95`
              : "no completed timings"
          }
          tone="neutral"
        />
        <SummaryMetric
          label="File ops"
          value={formatCount(summary.fileOpsTotal)}
          detail={
            summary.uniqueFiles > 0
              ? `${formatCount(summary.uniqueFiles)} files touched`
              : summary.fileOpsTotal > 0
                ? `${formatCount(summary.fileOpsTotal)} recorded ops`
                : "no file operations"
          }
          tone={summary.fileOpsTotal > 0 ? "amber" : "neutral"}
        />
      </SummaryStrip>

      <div className="grid gap-4 xl:grid-cols-[1fr_1fr]">
        <SectionPanel title="Source mix">
          {summary.sourceMix.length > 0 ? (
            <div className="space-y-3">
              {summary.sourceMix.map((source) => (
                <DistributionRow
                  key={source.source}
                  label={source.source}
                  value={`${formatCount(source.total)} (${source.percent}%)`}
                  percent={source.percent}
                  tone={toneForSource(source.source)}
                  detail={`${formatCount(source.success)} success / ${formatCount(
                    source.failure,
                  )} failed`}
                />
              ))}
            </div>
          ) : (
            <EmptyState title="No source data yet" />
          )}
        </SectionPanel>

        <SectionPanel title="Top tools">
          {topSkills.length > 0 ? (
            <div className="space-y-3">
              {topSkills.map((skill) => (
                <DistributionRow
                  key={skill.skillName}
                  label={skill.skillName}
                  value={formatCount(skill.total)}
                  percent={percent(skill.total, summary.totalActivities)}
                  tone={skill.failure > 0 ? "amber" : "emerald"}
                  detail={`${formatCount(skill.success)} success / ${formatCount(
                    skill.failure,
                  )} failed, ${formatDuration(skill.averageLatencyMs)} avg`}
                  mono
                />
              ))}
            </div>
          ) : (
            <EmptyState title="No tool activity yet" />
          )}
        </SectionPanel>
      </div>

      <div className="grid gap-4 xl:grid-cols-[0.85fr_1.15fr]">
        <SectionPanel title="File operations">
          <div className="grid gap-2 sm:grid-cols-4">
            <SummaryMetric label="create" value={formatCount(summary.fileCreates)} tone="emerald" />
            <SummaryMetric label="update" value={formatCount(summary.fileUpdates)} />
            <SummaryMetric label="delete" value={formatCount(summary.fileDeletes)} tone="red" />
            <SummaryMetric label="unique" value={formatCount(summary.uniqueFiles)} />
          </div>
          {topFiles.length > 0 ? (
            <div className="mt-3 space-y-2">
              {topFiles.map((file) => (
                <SurfacePanel
                  as="div"
                  key={file.path}
                  className="bg-runtime-panel/30 p-3 text-xs"
                >
                  <div className="truncate font-mono text-ink-soft">
                    {file.path}
                  </div>
                  <div className="mt-2 flex flex-wrap gap-2 text-[11px] text-ink-muted">
                    <span>{formatCount(file.total)} ops</span>
                    <span>{formatCount(file.creates)} create</span>
                    <span>{formatCount(file.updates)} update</span>
                    <span>{formatCount(file.deletes)} delete</span>
                  </div>
                </SurfacePanel>
              ))}
            </div>
          ) : (
            <div className="mt-3">
              <EmptyState title="No file operations recorded" />
            </div>
          )}
        </SectionPanel>

        <SectionPanel title="Recent calls">
          {recentActivities.length > 0 ? (
            <div className="space-y-2">
              {recentActivities.map((activity) => (
                <ActivityRow key={`${activity.kind}:${activity.id}`} activity={activity} />
              ))}
            </div>
          ) : (
            <EmptyState title="No calls recorded yet" />
          )}
        </SectionPanel>
      </div>
    </section>
  );
}

export function summarizeAgentInsights(
  agentName: string,
  callLogs: readonly AgentCallLog[],
  runs: readonly SubagentRun[],
  proofs: readonly AgentProofRun[],
): AgentInsightsSummary {
  const activities = [
    ...callLogs
      .filter((log) => log.agent_name === agentName)
      .map(activityFromCallLog),
    ...runs.filter((run) => run.agent_name === agentName).map(activityFromRun),
    ...proofs
      .filter((proof) => proof.agent_name === agentName)
      .map(activityFromProof),
  ].sort((a, b) => timestamp(b.createdAt) - timestamp(a.createdAt));

  const outcomes = activities.reduce(
    (acc, activity) => {
      acc[activity.outcome] += 1;
      return acc;
    },
    { success: 0, failure: 0, active: 0, pending: 0, unknown: 0 },
  );
  const terminal = outcomes.success + outcomes.failure;
  const latencies = activities
    .map((activity) => activity.elapsedMs)
    .filter(isFiniteNumber)
    .sort((a, b) => a - b);
  const fileOps = activities.flatMap((activity) => activity.fileOps);
  const fileOpsTotal = activities.reduce(
    (total, activity) => total + activity.fileOpsCount,
    0,
  );
  const fileTotals = summarizeFileOps(fileOps);

  return {
    agentName,
    totalActivities: activities.length,
    totalCalls: callLogs.filter((log) => log.agent_name === agentName).length,
    totalRuns: runs.filter((run) => run.agent_name === agentName).length,
    totalProofs: proofs.filter((proof) => proof.agent_name === agentName).length,
    success: outcomes.success,
    failure: outcomes.failure,
    active: outcomes.active,
    pending: outcomes.pending,
    unknown: outcomes.unknown,
    successRate: percent(outcomes.success, terminal),
    latencySamples: latencies.length,
    averageLatencyMs: average(latencies),
    medianLatencyMs: percentile(latencies, 50),
    p95LatencyMs: percentile(latencies, 95),
    fileOpsTotal,
    fileCreates: fileTotals.creates,
    fileUpdates: fileTotals.updates,
    fileDeletes: fileTotals.deletes,
    uniqueFiles: fileTotals.uniqueFiles,
    activities,
    topSkills: summarizeSkills(activities),
    sourceMix: summarizeSources(activities),
    topFiles: fileTotals.topFiles,
  };
}

function activityFromCallLog(log: AgentCallLog): AgentInsightActivity {
  return {
    id: log.id,
    kind: "call",
    source: log.source || "call",
    agentName: log.agent_name,
    skillName: log.skill_name || "unknown",
    status: log.status || "unknown",
    badge: log.badge,
    outcome: outcomeFor(log.status, log.badge),
    summary: log.summary,
    error: log.error,
    elapsedMs:
      log.elapsed_ms ?? elapsedFromDates(log.started_at, log.completed_at, log.created_at),
    fileOps: log.file_ops,
    fileOpsCount: log.file_ops.length,
    createdAt: log.created_at,
    startedAt: log.started_at,
    completedAt: log.completed_at,
  };
}

function activityFromRun(run: SubagentRun): AgentInsightActivity {
  return {
    id: run.grant_id,
    kind: "run",
    source: "subagent",
    agentName: run.agent_name,
    skillName: run.skill_name || "unknown",
    status: run.status || "unknown",
    badge: null,
    outcome: outcomeFor(run.status, null),
    summary: run.summary,
    error: null,
    elapsedMs: elapsedFromDates(null, run.completed_at, run.created_at),
    fileOps: run.file_ops,
    fileOpsCount: run.file_ops_count ?? run.file_ops.length,
    createdAt: run.created_at,
    startedAt: null,
    completedAt: run.completed_at,
  };
}

function activityFromProof(proof: AgentProofRun): AgentInsightActivity {
  return {
    id: String(proof.id),
    kind: "proof",
    source: "proof",
    agentName: proof.agent_name,
    skillName: proof.skill_name || "unknown",
    status: proof.status || "unknown",
    badge: proof.badge,
    outcome: outcomeFor(proof.status, proof.badge),
    summary: proof.summary,
    error: proof.error,
    elapsedMs:
      proof.elapsed_ms ??
      elapsedFromDates(proof.started_at, proof.completed_at, proof.created_at),
    fileOps: proof.file_ops,
    fileOpsCount: proof.file_ops_count ?? proof.file_ops.length,
    createdAt: proof.created_at,
    startedAt: proof.started_at,
    completedAt: proof.completed_at,
  };
}

function summarizeSkills(
  activities: readonly AgentInsightActivity[],
): AgentInsightSkillStat[] {
  const stats = new Map<
    string,
    { total: number; success: number; failure: number; latencies: number[] }
  >();

  for (const activity of activities) {
    const existing = stats.get(activity.skillName) || {
      total: 0,
      success: 0,
      failure: 0,
      latencies: [],
    };
    existing.total += 1;
    if (activity.outcome === "success") existing.success += 1;
    if (activity.outcome === "failure") existing.failure += 1;
    if (activity.elapsedMs !== null) existing.latencies.push(activity.elapsedMs);
    stats.set(activity.skillName, existing);
  }

  return [...stats.entries()]
    .map(([skillName, stat]) => ({
      skillName,
      total: stat.total,
      success: stat.success,
      failure: stat.failure,
      averageLatencyMs: average(stat.latencies),
    }))
    .sort((a, b) => b.total - a.total || b.success - a.success || a.skillName.localeCompare(b.skillName));
}

function summarizeSources(
  activities: readonly AgentInsightActivity[],
): AgentInsightSourceStat[] {
  const stats = new Map<string, { total: number; success: number; failure: number }>();

  for (const activity of activities) {
    const existing = stats.get(activity.source) || {
      total: 0,
      success: 0,
      failure: 0,
    };
    existing.total += 1;
    if (activity.outcome === "success") existing.success += 1;
    if (activity.outcome === "failure") existing.failure += 1;
    stats.set(activity.source, existing);
  }

  return [...stats.entries()]
    .map(([source, stat]) => ({
      source,
      total: stat.total,
      success: stat.success,
      failure: stat.failure,
      percent: percent(stat.total, activities.length),
    }))
    .sort((a, b) => b.total - a.total || a.source.localeCompare(b.source));
}

function summarizeFileOps(fileOps: readonly SubagentFileOp[]) {
  const files = new Map<string, AgentInsightFileStat>();
  let creates = 0;
  let updates = 0;
  let deletes = 0;

  for (const fileOp of fileOps) {
    const op = fileOp.op.toLowerCase();
    if (op === "create") creates += 1;
    if (op === "update") updates += 1;
    if (op === "delete") deletes += 1;

    const existing = files.get(fileOp.path) || {
      path: fileOp.path,
      total: 0,
      creates: 0,
      updates: 0,
      deletes: 0,
    };
    existing.total += 1;
    if (op === "create") existing.creates += 1;
    if (op === "update") existing.updates += 1;
    if (op === "delete") existing.deletes += 1;
    files.set(fileOp.path, existing);
  }

  return {
    creates,
    updates,
    deletes,
    uniqueFiles: files.size,
    topFiles: [...files.values()].sort(
      (a, b) => b.total - a.total || a.path.localeCompare(b.path),
    ),
  };
}

function DistributionRow({
  label,
  value,
  percent,
  tone,
  detail,
  mono = false,
}: {
  label: string;
  value: string;
  percent: number;
  tone: Tone;
  detail: string;
  mono?: boolean;
}) {
  return (
    <div>
      <div className="flex min-w-0 items-center justify-between gap-3 text-xs">
        <div
          className={`min-w-0 truncate text-ink-soft ${
            mono ? "font-mono" : ""
          }`}
        >
          {label}
        </div>
        <div className="shrink-0 font-mono text-ink-dim">{value}</div>
      </div>
      <ProgressBar
        value={percent}
        tone={badgeToneForInsight(tone)}
        aria-label={`${label} distribution`}
        className="mt-1"
      />
      <div className="mt-1 truncate text-[11px] text-ink-faint">{detail}</div>
    </div>
  );
}

function ActivityRow({ activity }: { activity: AgentInsightActivity }) {
  return (
    <SurfacePanel as="article" className="bg-runtime-panel/30 p-3 text-xs">
      <div className="grid gap-3 md:grid-cols-[150px_1fr_88px_76px] md:items-start">
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          <StatusBadge tone={badgeToneForInsight(toneForSource(activity.source))}>
            {activity.source}
          </StatusBadge>
          <StatusBadge>{activity.kind}</StatusBadge>
        </div>
        <div className="min-w-0">
          <div className="flex min-w-0 flex-wrap items-center gap-2">
            <span className="truncate font-mono text-ink">
              {activity.skillName}
            </span>
            <StateBadge
              status={activity.status}
              tone={outcomeTone(activity.outcome)}
              live={activity.outcome === "active"}
              size="xs"
            />
            {activity.badge && (
              <span className="text-[11px] text-ink-muted">
                {activity.badge}
              </span>
            )}
          </div>
          <div className="mt-1 truncate text-ink-muted">
            {activity.summary || activity.error || "No summary."}
          </div>
        </div>
        <div className="font-mono text-ink-muted">
          {formatDuration(activity.elapsedMs)}
        </div>
        <div className="text-ink-faint">{formatDate(activity.createdAt)}</div>
      </div>
      {activity.fileOps.length > 0 && (
        <div className="mt-2 truncate text-[11px] text-ink-faint">
          {formatCount(activity.fileOps.length)} file ops
        </div>
      )}
    </SurfacePanel>
  );
}

function outcomeFor(status: string, badge: string | null): AgentInsightOutcome {
  const normalizedStatus = status.toLowerCase();
  const normalizedBadge = badge?.toLowerCase() || null;

  if (normalizedBadge === "degraded" || normalizedBadge === "unverified") {
    return "failure";
  }
  if (normalizedBadge === "verified") return "success";
  if (isFailureStatus(normalizedStatus)) return "failure";
  if (isSuccessStatus(normalizedStatus)) return "success";
  if (isActiveStatus(normalizedStatus)) return "active";
  if (isPendingStatus(normalizedStatus)) return "pending";
  return "unknown";
}

function isSuccessStatus(status: string) {
  return [
    "complete",
    "completed",
    "ok",
    "passed",
    "success",
    "succeeded",
    "verified",
  ].includes(status);
}

function isFailureStatus(status: string) {
  return [
    "cancelled",
    "canceled",
    "denied",
    "degraded",
    "error",
    "failed",
    "failure",
    "timed_out",
    "timeout",
    "unverified",
  ].includes(status);
}

function isActiveStatus(status: string) {
  return ["active", "building", "deploying", "running", "started", "verifying"].includes(
    status,
  );
}

function isPendingStatus(status: string) {
  return ["pending", "queued", "scheduled", "waiting"].includes(status);
}

function toneForSource(source: string): Tone {
  const normalized = source.toLowerCase();
  if (normalized === "handoff" || normalized === "subagent") return "neutral";
  if (normalized === "trial") return "amber";
  if (normalized === "proof") return "emerald";
  if (normalized === "api") return "amber";
  return "neutral";
}

function badgeToneForInsight(tone: Tone): StatusBadgeTone {
  if (tone === "emerald" || tone === "amber" || tone === "red") return tone;
  return "neutral";
}

function outcomeTone(outcome: AgentInsightOutcome): StatusBadgeTone {
  if (outcome === "success") return "emerald";
  if (outcome === "failure") return "red";
  if (outcome === "active" || outcome === "pending") return "amber";
  return "neutral";
}

function elapsedFromDates(
  startedAt: string | null,
  completedAt: string | null,
  createdAt: string,
) {
  if (!completedAt) return null;
  const start = timestamp(startedAt || createdAt);
  const end = timestamp(completedAt);
  const elapsed = end - start;
  return Number.isFinite(elapsed) && elapsed >= 0 ? elapsed : null;
}

function timestamp(value: string | null) {
  if (!value) return 0;
  const time = new Date(value).getTime();
  return Number.isNaN(time) ? 0 : time;
}

function average(values: readonly number[]) {
  if (values.length === 0) return null;
  return values.reduce((sum, value) => sum + value, 0) / values.length;
}

function percentile(values: readonly number[], target: number) {
  if (values.length === 0) return null;
  const index = Math.ceil((target / 100) * values.length) - 1;
  return values[Math.max(0, Math.min(values.length - 1, index))];
}

function percent(value: number, total: number) {
  if (total <= 0) return 0;
  return Math.round((value / total) * 100);
}

function isFiniteNumber(value: number | null): value is number {
  return value !== null && Number.isFinite(value);
}

function formatCount(value: number) {
  return NUMBER_FORMAT.format(value);
}

function formatDuration(value: number | null) {
  if (value === null || !Number.isFinite(value)) return "-";
  if (value < 1000) return `${Math.round(value)}ms`;
  if (value < 60_000) return `${Math.round(value / 100) / 10}s`;
  return `${Math.round(value / 6000) / 10}m`;
}

function formatDate(value: string) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "unknown";
  return date.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}
