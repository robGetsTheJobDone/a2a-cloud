import { useCallback, useId, useRef, useState, type ReactNode } from "react";
import {
  getAgentDeploymentLogs,
  type AgentDeployment,
  type AgentDeploymentEvent,
  type AgentDeploymentLog,
} from "../api";
import {
  CopyButton,
  InlineAlert,
  SummaryMetric,
  SurfacePanel,
  ToolbarLink,
  type StatusBadgeTone,
} from "./DashboardChrome";
import { StatusBadge } from "./StatusPillAdapters";

type AgentDeploymentTimelineAgent = {
  name?: string | null;
  repo_url?: string | null;
  image?: string | null;
  url?: string | null;
};

export type AgentDeploymentTimelineProps = {
  deployment: AgentDeployment | null;
  agent?: AgentDeploymentTimelineAgent;
  title?: string;
  className?: string;
  eventLimit?: number;
  emptyMessage?: string;
};

type StageStatus = "passed" | "failed" | "running" | "pending";
type StageId = (typeof DEPLOYMENT_STAGES)[number]["id"];

type StageView = {
  id: StageId;
  label: string;
  status: StageStatus;
  statusLabel: string;
  message: string;
  event: AgentDeploymentEvent | null;
  current: boolean;
};

const DEPLOYMENT_STAGES = [
  {
    id: "source",
    label: "Prepare source",
    pending: "Waiting for source to be uploaded or pushed.",
  },
  {
    id: "build",
    label: "Build image",
    pending: "Waiting for the image builder.",
  },
  {
    id: "runtime",
    label: "Start runtime",
    pending: "Waiting for runtime pods.",
  },
  {
    id: "agent_card",
    label: "Verify agent card",
    pending: "Waiting for the agent-card endpoint.",
  },
  {
    id: "skills",
    label: "Verify tools",
    pending: "Waiting for callable tools.",
  },
  {
    id: "verify",
    label: "Go live",
    pending: "Waiting for final verification.",
  },
] as const;

const ACTIVE_DEPLOYMENT_STATUSES = new Set([
  "queued",
  "building",
  "deploying",
  "verifying",
]);
const INTERNAL_EVENT_STAGES = new Set(["argo"]);
// Timeline stages that can carry proxied raw logs, mapped to their log sources.
const STAGE_LOG_SOURCES: Partial<Record<StageId, string[]>> = {
  build: ["gitea_actions"],
  runtime: ["pod", "argo"],
};

function deploymentTriggerLabel(trigger?: string | null): string {
  return trigger ? trigger.replaceAll("_", " ") : "unknown";
}

export function AgentDeploymentTimeline({
  deployment,
  agent,
  title = "Latest deployment",
  className = "",
  eventLimit = 8,
  emptyMessage = "No deployment run has been recorded for this agent yet. The next source deploy or runtime bump will create a live timeline here.",
}: AgentDeploymentTimelineProps) {
  const titleId = useId();
  const descriptionId = useId();
  // Called unconditionally (before the early return) to satisfy the rules of
  // hooks; args fall back to empty strings when no deployment is present.
  const deploymentLogs = useDeploymentLogs(
    agent?.name || deployment?.agent_name || "",
    deployment?.deploy_id || "",
  );
  const classes = ["p-3", className]
    .filter(Boolean)
    .join(" ");

  if (!deployment) {
    return (
      <SurfacePanel as="section" aria-labelledby={titleId} className={classes}>
        <div
          id={titleId}
          className="text-[10px] uppercase text-ink-faint"
        >
          {title}
        </div>
        <div role="status" className="mt-2 text-xs leading-relaxed text-ink-muted">
          {emptyMessage}
        </div>
      </SurfacePanel>
    );
  }

  const events = sortEvents(deployment.events);
  const visibleEvents = userVisibleEvents(events);
  const stages = buildStageViews(deployment, visibleEvents);
  const progress = summarizeProgress(deployment, stages);
  const progressPercent = Math.max(
    0,
    Math.min(100, Math.round((progress.value / progress.max) * 100)),
  );
  const sourceUrl = deployment.source_repo_url || agent?.repo_url || null;
  const liveUrl = deployment.agent_url || agent?.url || null;
  const image = deployment.image || agent?.image || null;
  const commitUrl =
    sourceUrl && deployment.head_sha
      ? commitUrlForRepo(sourceUrl, deployment.head_sha)
      : null;
  const maxEvents =
    Number.isFinite(eventLimit) && eventLimit > 0
      ? Math.floor(eventLimit)
      : visibleEvents.length;
  const recentEvents = visibleEvents.slice(-maxEvents).reverse();
  const failures = failureMessages(deployment, visibleEvents);
  const agentName = agent?.name || deployment.agent_name;

  return (
    <SurfacePanel
      as="section"
      aria-labelledby={titleId}
      aria-describedby={descriptionId}
      className={classes}
    >
      <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <div
              id={titleId}
              className="text-[10px] uppercase text-ink-faint"
            >
              {title}
            </div>
            <DeploymentStatusBadge status={deployment.status} />
            <EvidenceToken value={deployment.deploy_id} copyLabel="Copy deployment id" />
          </div>
          <p
            id={descriptionId}
            className="mt-2 max-w-3xl text-sm leading-relaxed text-ink-soft"
          >
            {deploymentSummary(deployment, progress.currentStage)}
          </p>
        </div>

        <div className="flex flex-wrap gap-2 text-xs">
          {sourceUrl && <ExternalLink href={sourceUrl}>Repo</ExternalLink>}
          {commitUrl && <ExternalLink href={commitUrl}>Commit</ExternalLink>}
          {liveUrl && <ExternalLink href={liveUrl}>Live URL</ExternalLink>}
          {deployment.head_sha && !commitUrl && (
            <StatusBadge tone="neutral" className="rounded-md px-2.5 py-1.5 font-mono">
              {shortSha(deployment.head_sha)}
            </StatusBadge>
          )}
        </div>
      </div>

      {failures.length > 0 && (
        <InlineAlert
          tone="red"
          role="alert"
          className="mt-3 text-xs"
        >
          <div className="font-semibold text-signal-danger">Deployment error</div>
          <ul className="mt-1 space-y-1">
            {failures.map((failure) => (
              <li key={failure}>{failure}</li>
            ))}
          </ul>
        </InlineAlert>
      )}

      <div className="mt-4 grid gap-4 lg:grid-cols-[1.15fr_0.85fr]">
        <div>
          <div
            role="progressbar"
            aria-label={`${agentName} deployment progress`}
            aria-valuemin={0}
            aria-valuemax={progress.max}
            aria-valuenow={progress.value}
            aria-valuetext={progress.text}
            className="rounded-full bg-runtime-raised"
          >
            <div
              aria-hidden="true"
              className={progressBarClass(deployment.status)}
              style={{ width: `${progressPercent}%` }}
            />
          </div>
          <div className="mt-2 text-[11px] text-ink-muted">
            {progress.text}
          </div>

          <ol className="mt-4 space-y-2" aria-label="Deployment stages">
            {stages.map((stage) => (
              <li
                key={stage.id}
                aria-current={stage.current ? "step" : undefined}
                className="grid grid-cols-[24px_1fr] gap-3 border-l border-runtime-line-soft/60 pl-3"
              >
                <StageMarker status={stage.status} />
                <div className="min-w-0 pb-3">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-xs font-semibold text-ink-soft">
                      {stage.label}
                    </span>
                    <StatusBadge
                      tone={stageStatusTone(stage.status)}
                      className="min-h-4 px-1.5 text-[10px]"
                    >
                      {stage.statusLabel}
                    </StatusBadge>
                    {stage.event && (
                      <time
                        dateTime={stage.event.created_at}
                        className="text-[11px] text-ink-faint"
                      >
                        {fmtDateTime(stage.event.created_at)}
                      </time>
                    )}
                  </div>
                  <div className="mt-1 text-xs leading-relaxed text-ink-muted">
                    {stage.message}
                  </div>
                  {STAGE_LOG_SOURCES[stage.id] && (
                    <StageLogDisclosure
                      sources={STAGE_LOG_SOURCES[stage.id] as string[]}
                      logs={deploymentLogs.logs}
                      state={deploymentLogs.state}
                      error={deploymentLogs.error}
                      onOpen={deploymentLogs.load}
                    />
                  )}
                </div>
              </li>
            ))}
          </ol>
        </div>

        <div className="space-y-4">
          <SurfacePanel as="dl" className="space-y-2 p-3 text-xs">
            <MetaRow
              label="deploy id"
              value={deployment.deploy_id}
              copyValue={deployment.deploy_id}
              mono
            />
            <MetaRow label="status" value={deployment.status} />
            <MetaRow label="trigger" value={deploymentTriggerLabel(deployment.trigger)} />
            <MetaRow
              label="image"
              value={image || "pending"}
              copyValue={image}
              mono
            />
            <MetaRow
              label="head"
              copyValue={deployment.head_sha}
              value={
                deployment.head_sha ? (
                  commitUrl ? (
                    <ExternalTextLink href={commitUrl}>
                      {shortSha(deployment.head_sha)}
                    </ExternalTextLink>
                  ) : (
                    shortSha(deployment.head_sha)
                  )
                ) : (
                  "pending"
                )
              }
              mono
            />
            <MetaRow
              label="repo"
              copyValue={sourceUrl}
              value={
                sourceUrl ? (
                  <ExternalTextLink href={sourceUrl}>
                    {compactUrl(sourceUrl)}
                  </ExternalTextLink>
                ) : (
                  "not linked"
                )
              }
            />
            <MetaRow
              label="live"
              copyValue={liveUrl}
              value={
                liveUrl ? (
                  <ExternalTextLink href={liveUrl}>{compactUrl(liveUrl)}</ExternalTextLink>
                ) : deployment.status === "live" ? (
                  "live URL pending"
                ) : (
                  "pending"
                )
              }
            />
            <MetaRow
              label="started"
              value={fmtDateTime(deployment.started_at || deployment.created_at)}
            />
            <MetaRow label="updated" value={fmtDateTime(deployment.updated_at)} />
            <MetaRow
              label="completed"
              value={fmtDateTime(deployment.completed_at)}
            />
          </SurfacePanel>

          <SurfacePanel
            as="section"
            aria-label="Deployment events"
            className="p-3"
          >
            <div className="flex items-center justify-between gap-3">
              <div className="text-[10px] uppercase text-ink-faint">
                Events
              </div>
              <div className="font-mono text-[11px] text-ink-faint">
                {visibleEvents.length}
              </div>
            </div>
            {recentEvents.length === 0 ? (
              <div className="mt-3 text-xs text-ink-muted">
                No deployment events have arrived yet.
              </div>
            ) : (
              <ol className="mt-3 space-y-2">
                {recentEvents.map((event) => {
                  const dataSummary = eventDataSummary(event.data);
                  return (
                    <SurfacePanel
                      as="li"
                      key={event.id}
                      className="bg-runtime-bg/40 p-2 text-xs"
                    >
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="font-mono text-ink-soft">
                          {stageLabel(event.stage)}
                        </span>
                        <StatusBadge
                          tone={stageStatusTone(
                            normalizeStageStatus(event.status) || "pending",
                          )}
                          className="min-h-4 px-1.5 text-[10px]"
                        >
                          {event.status || "pending"}
                        </StatusBadge>
                        <time
                          dateTime={event.created_at}
                          className="ml-auto text-[11px] text-ink-faint"
                        >
                          {fmtDateTime(event.created_at)}
                        </time>
                      </div>
                      <div className="mt-1 leading-relaxed text-ink-muted">
                        {event.message || "No event message."}
                      </div>
                      {dataSummary && (
                        <div className="mt-1 font-mono text-[11px] text-ink-faint [overflow-wrap:anywhere]">
                          {dataSummary}
                        </div>
                      )}
                    </SurfacePanel>
                  );
                })}
              </ol>
            )}
          </SurfacePanel>
        </div>
      </div>
    </SurfacePanel>
  );
}

type LogLoadState = "idle" | "loading" | "loaded" | "error";

function useDeploymentLogs(agentName: string, deployId: string) {
  const [logs, setLogs] = useState<AgentDeploymentLog[]>([]);
  const [state, setState] = useState<LogLoadState>("idle");
  const [error, setError] = useState<string | null>(null);
  const requestedFor = useRef<string | null>(null);

  const load = useCallback(async () => {
    if (!agentName || !deployId) return;
    if (requestedFor.current === deployId) return;
    requestedFor.current = deployId;
    setState("loading");
    setError(null);
    try {
      const res = await getAgentDeploymentLogs(agentName, deployId);
      setLogs(res.logs);
      setState("loaded");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load logs.");
      setState("error");
      requestedFor.current = null; // allow retry on next open
    }
  }, [agentName, deployId]);

  return { logs, state, error, load };
}

function StageLogDisclosure({
  sources,
  logs,
  state,
  error,
  onOpen,
}: {
  sources: string[];
  logs: AgentDeploymentLog[];
  state: LogLoadState;
  error: string | null;
  onOpen: () => void;
}) {
  const matched = logs.filter((log) => sources.includes(log.source));
  return (
    <details
      className="group mt-2"
      onToggle={(event) => {
        if ((event.currentTarget as HTMLDetailsElement).open) onOpen();
      }}
    >
      <summary className="inline-flex cursor-pointer list-none items-center gap-1 text-[11px] text-signal-protocol focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-signal-protocol">
        <span className="transition-transform group-open:rotate-90" aria-hidden="true">
          ›
        </span>
        View logs
      </summary>
      <div className="mt-2 space-y-2">
        {state === "loading" && (
          <div className="text-[11px] text-ink-faint">Loading logs…</div>
        )}
        {state === "error" && (
          <div className="text-[11px] text-signal-danger">
            {error || "Failed to load logs."}
          </div>
        )}
        {matched.map((log) => (
          <LogBlock key={log.source} log={log} />
        ))}
        {state === "loaded" && matched.length === 0 && (
          <div className="text-[11px] text-ink-faint">
            No logs were captured for this stage.
          </div>
        )}
      </div>
    </details>
  );
}

function LogBlock({ log }: { log: AgentDeploymentLog }) {
  return (
    <div>
      <div className="flex items-center gap-2 text-[10px] uppercase text-ink-faint">
        <span>{logSourceLabel(log.source)}</span>
        {log.truncated && (
          <span className="text-signal-authority">truncated</span>
        )}
        <CopyButton
          value={log.content}
          label="Copy logs"
          copiedLabel="copied"
          className="ml-auto h-5 px-1.5 text-[10px]"
        >
          copy
        </CopyButton>
      </div>
      <pre className="mt-1 max-h-72 overflow-auto whitespace-pre-wrap rounded-md bg-runtime-bg/60 p-2 font-mono text-[11px] leading-relaxed text-ink-soft [overflow-wrap:anywhere]">
        {log.content}
      </pre>
    </div>
  );
}

function logSourceLabel(source: string): string {
  if (source === "gitea_actions") return "Build · Gitea Actions";
  if (source === "pod") return "Runtime · pod logs";
  if (source === "argo") return "Argo · sync";
  return source;
}

function buildStageViews(
  deployment: AgentDeployment,
  events: AgentDeploymentEvent[],
): StageView[] {
  const latest = latestEventsByStage(events);
  const currentStageId = currentStage(deployment, latest);
  const currentIndex = stageIndex(currentStageId);
  const deploymentLive = deployment.status === "live";
  const deploymentFailed = deployment.status === "failed";
  const deploymentActive = ACTIVE_DEPLOYMENT_STATUSES.has(deployment.status);

  return DEPLOYMENT_STAGES.map((stage, index) => {
    const event = latest[stage.id] || null;
    const normalizedEventStatus = normalizeStageStatus(event?.status);
    let status: StageStatus;

    if (deploymentLive) {
      status = "passed";
    } else if (normalizedEventStatus === "failed") {
      status = "failed";
    } else if ((deploymentActive || deploymentFailed) && index < currentIndex) {
      status = "passed";
    } else if (normalizedEventStatus === "passed") {
      status = "passed";
    } else if (normalizedEventStatus === "running") {
      status = "running";
    } else if (deploymentFailed && stage.id === currentStageId) {
      status = "failed";
    } else if (deploymentActive && stage.id === currentStageId) {
      status = "running";
    } else {
      status = "pending";
    }

    return {
      id: stage.id,
      label: stage.label,
      status,
      statusLabel: normalizedEventStatus === status ? event?.status || status : status,
      message:
        normalizedEventStatus === status && event?.message
          ? event.message
          : stageMessage(stage.pending, status, deployment.status),
      event,
      current:
        stage.id === currentStageId &&
        deployment.status !== "live" &&
        (status === "running" || status === "failed" || status === "pending"),
    };
  });
}

function latestEventsByStage(
  events: AgentDeploymentEvent[],
): Partial<Record<StageId, AgentDeploymentEvent>> {
  return events.reduce<Partial<Record<StageId, AgentDeploymentEvent>>>(
    (acc, event) => {
      if (isStageId(event.stage)) acc[event.stage] = event;
      return acc;
    },
    {},
  );
}

function userVisibleEvents(events: AgentDeploymentEvent[]): AgentDeploymentEvent[] {
  return events.filter((event) => !INTERNAL_EVENT_STAGES.has(event.stage));
}

function currentStage(
  deployment: AgentDeployment,
  latest: Partial<Record<StageId, AgentDeploymentEvent>>,
): StageId {
  const latestStageEvents = sortEvents(
    Object.values(latest).filter(
      (event): event is AgentDeploymentEvent => Boolean(event),
    ),
  );
  const passedIndex = furthestPassedStageIndex(latest);
  const failedEvent = [...latestStageEvents]
    .reverse()
    .find(
      (event) =>
        isStageId(event.stage) &&
        normalizeStageStatus(event.status) === "failed" &&
        stageIndex(event.stage) >= passedIndex,
    );
  if (failedEvent && isStageId(failedEvent.stage)) return failedEvent.stage;

  const runningEvent = [...latestStageEvents]
    .reverse()
    .find(
      (event) =>
        isStageId(event.stage) &&
        normalizeStageStatus(event.status) === "running" &&
        stageIndex(event.stage) >= passedIndex,
    );
  if (runningEvent && isStageId(runningEvent.stage)) return runningEvent.stage;

  if (deployment.status === "queued") return "source";
  if (deployment.status === "building") return "build";
  if (deployment.status === "deploying") {
    return firstIncompleteStage(["runtime"], latest) || "runtime";
  }
  if (deployment.status === "verifying") {
    return firstIncompleteStage(["agent_card", "skills", "verify"], latest) || "verify";
  }
  if (deployment.status === "live") return "verify";

  const latestKnownEvent = [...latestStageEvents]
    .reverse()
    .find((event) => isStageId(event.stage));
  return latestKnownEvent && isStageId(latestKnownEvent.stage)
    ? latestKnownEvent.stage
    : "source";
}

function furthestPassedStageIndex(
  latest: Partial<Record<StageId, AgentDeploymentEvent>>,
): number {
  return DEPLOYMENT_STAGES.reduce((furthest, stage, index) => {
    return normalizeStageStatus(latest[stage.id]?.status) === "passed"
      ? Math.max(furthest, index)
      : furthest;
  }, 0);
}

function firstIncompleteStage(
  stages: StageId[],
  latest: Partial<Record<StageId, AgentDeploymentEvent>>,
): StageId | null {
  return (
    stages.find((stage) => normalizeStageStatus(latest[stage]?.status) !== "passed") ||
    null
  );
}

function summarizeProgress(deployment: AgentDeployment, stages: StageView[]) {
  const max = stages.length;
  const passed = stages.filter((stage) => stage.status === "passed").length;
  const current =
    stages.find((stage) => stage.current) ||
    stages.find((stage) => stage.status === "running") ||
    stages.find((stage) => stage.status === "failed") ||
    stages.find((stage) => stage.status === "pending") ||
    null;

  if (deployment.status === "live") {
    return {
      max,
      value: max,
      currentStage: current,
      text: `Deployment is live. ${max} of ${max} stages passed.`,
    };
  }

  if (deployment.status === "failed") {
    return {
      max,
      value: passed,
      currentStage: current,
      text: current
        ? `Deployment failed at ${current.label}. ${passed} of ${max} stages passed.`
        : `Deployment failed. ${passed} of ${max} stages passed.`,
    };
  }

  if (current) {
    return {
      max,
      value: passed,
      currentStage: current,
      text: `${passed} of ${max} stages passed. ${current.label} is ${current.status}.`,
    };
  }

  return {
    max,
    value: passed,
    currentStage: null,
    text: `${passed} of ${max} stages passed.`,
  };
}

function deploymentSummary(
  deployment: AgentDeployment,
  currentStage: StageView | null,
): string {
  if (deployment.status === "live") {
    return deployment.completed_at
      ? `Live and verified since ${fmtDateTime(deployment.completed_at)}.`
      : "Live and verified from inside the cluster.";
  }
  if (deployment.status === "failed") {
    return deployment.error || "Deployment needs attention.";
  }
  if (deployment.status === "queued") {
    return "Queued and waiting for deployment workers.";
  }
  if (currentStage) {
    return `${currentStage.label} is ${currentStage.status}; build and runtime verification will continue until the agent is live.`;
  }
  return "Build and runtime verification are still in progress.";
}

function failureMessages(
  deployment: AgentDeployment,
  events: AgentDeploymentEvent[],
): string[] {
  const messages = new Set<string>();
  const failed = deployment.status === "failed";
  if (deployment.error) messages.add(deployment.error);

  for (const event of events) {
    const eventFailed = normalizeStageStatus(event.status) === "failed";
    if (eventFailed && event.message) {
      messages.add(`${stageLabel(event.stage)}: ${event.message}`);
    }
    const error = failed || eventFailed ? stringField(event.data, "error") : null;
    if (error) {
      messages.add(`${stageLabel(event.stage)}: ${error}`);
    }
  }

  if (failed) {
    for (const [key, value] of Object.entries(deployment.verification || {})) {
      if (INTERNAL_EVENT_STAGES.has(key)) continue;
      const record = recordValue(value);
      const error = stringField(record, "error");
      if (error) messages.add(`${verificationLabel(key)}: ${error}`);
    }
  }

  return [...messages].slice(0, 4);
}

function sortEvents(events: AgentDeploymentEvent[]): AgentDeploymentEvent[] {
  return [...events].sort((a, b) => {
    const left = Date.parse(a.created_at);
    const right = Date.parse(b.created_at);
    if (Number.isNaN(left) || Number.isNaN(right)) return a.id - b.id;
    return left - right || a.id - b.id;
  });
}

function normalizeStageStatus(status?: string | null): StageStatus | null {
  if (!status) return null;
  const value = status.toLowerCase();
  if (["passed", "complete", "completed", "success", "succeeded", "live"].includes(value)) {
    return "passed";
  }
  if (["failed", "error", "errored", "denied"].includes(value)) return "failed";
  if (
    ["running", "building", "deploying", "verifying", "in_progress"].includes(value)
  ) {
    return "running";
  }
  if (["pending", "queued", "waiting"].includes(value)) return "pending";
  return null;
}

function stageMessage(
  pendingMessage: string,
  status: StageStatus,
  deploymentStatus: string,
): string {
  if (status === "passed") return "Completed.";
  if (status === "failed") return "Failed before this stage reported details.";
  if (status === "running") return `Deployment is ${deploymentStatus}.`;
  return pendingMessage;
}

function eventDataSummary(data: Record<string, unknown>): string | null {
  const error = stringField(data, "error");
  if (error) return error;

  const expectedRevision = stringField(data, "expected_revision");
  const revision = stringField(data, "revision");
  if (expectedRevision && revision && expectedRevision !== revision) {
    return `stale revision ${shortSha(revision)}; waiting for ${shortSha(expectedRevision)}`;
  }

  const expectedImage = stringField(data, "expected_image");
  const liveImage =
    stringField(data, "agent_image_env") ||
    (Array.isArray(data.images) && typeof data.images[0] === "string"
      ? data.images[0]
      : null);
  if (expectedImage && liveImage && expectedImage !== liveImage) {
    return "stale runtime image recorded";
  }

  const ready = numberField(data, "ready");
  const desired = numberField(data, "desired");
  if (ready !== null && desired !== null) return `pods ${ready}/${desired} ready`;

  const status = stringField(data, "status");
  const sync = stringField(data, "sync");
  const health = stringField(data, "health");
  if (sync && health) return `${sync} / ${health}`;
  if (status) return status;

  const count = numberField(data, "count");
  if (count !== null) return `${count} detected`;

  const keys = Object.keys(data);
  return keys.length > 0 ? `${keys.slice(0, 4).join(", ")} recorded` : null;
}

function stringField(record: Record<string, unknown>, key: string): string | null {
  const value = record[key];
  return typeof value === "string" && value.trim() ? value : null;
}

function numberField(record: Record<string, unknown>, key: string): number | null {
  const value = record[key];
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function recordValue(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function isStageId(stage: string): stage is StageId {
  return DEPLOYMENT_STAGES.some((item) => item.id === stage);
}

function stageIndex(stage: StageId): number {
  return DEPLOYMENT_STAGES.findIndex((item) => item.id === stage);
}

function stageLabel(stage: string): string {
  const known = DEPLOYMENT_STAGES.find((item) => item.id === stage);
  return known?.label || stage;
}

function verificationLabel(key: string): string {
  if (key === "agent_card") return "Agent card";
  return key
    .split("_")
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

function commitUrlForRepo(repoUrl: string, sha: string): string | null {
  if (!isHttpUrl(repoUrl)) return null;
  const base = repoUrl.replace(/\.git$/, "").replace(/\/$/, "");
  return repoUrl.includes("gitlab.") || repoUrl.includes("gitlab.com")
    ? `${base}/-/commit/${sha}`
    : `${base}/commit/${sha}`;
}

function isHttpUrl(value: string): boolean {
  return value.startsWith("http://") || value.startsWith("https://");
}

function compactUrl(value: string): string {
  if (!isHttpUrl(value)) return value;
  try {
    const url = new URL(value);
    return `${url.host}${url.pathname.replace(/\/$/, "")}`;
  } catch {
    return value;
  }
}

function shortSha(value: string): string {
  return value.slice(0, 8);
}

function progressBarClass(status: string): string {
  const tone =
    status === "live"
      ? "bg-signal-live"
      : status === "failed"
      ? "bg-signal-danger"
      : "bg-signal-authority";
  return `h-1.5 rounded-full ${tone}`;
}

function stageStatusTone(status: StageStatus): StatusBadgeTone {
  if (status === "passed") return "emerald";
  if (status === "failed") return "red";
  if (status === "running") return "amber";
  return "neutral";
}

function StageMarker({ status }: { status: StageStatus }) {
  const color =
    status === "passed"
      ? "bg-signal-live"
      : status === "failed"
      ? "bg-signal-danger"
      : status === "running"
      ? "bg-signal-authority"
      : "bg-runtime-line";
  return (
    <div
      aria-hidden="true"
      className="mt-0.5 flex h-5 w-5 items-center justify-center rounded-full border border-runtime-line-soft/60 bg-runtime-bg"
    >
      <span className={`h-2 w-2 rounded-full ${color}`} />
    </div>
  );
}

function DeploymentStatusBadge({ status }: { status: string }) {
  return (
    <StatusBadge tone={deploymentStatusTone(status)} dot={ACTIVE_DEPLOYMENT_STATUSES.has(status)}>
      deploy {status}
    </StatusBadge>
  );
}

function deploymentStatusTone(status: string): StatusBadgeTone {
  if (status === "live") return "emerald";
  if (status === "failed") return "red";
  if (ACTIVE_DEPLOYMENT_STATUSES.has(status)) return "amber";
  return "neutral";
}

function MetaRow({
  label,
  value,
  copyValue,
  mono = false,
}: {
  label: string;
  value: ReactNode;
  copyValue?: string | null;
  mono?: boolean;
}) {
  return (
    <SummaryMetric
      label={label}
      value={value}
      size="compact"
      mono={mono}
      className="bg-runtime-panel/40"
    >
      {copyValue && (
        <div className="mt-2">
          <CopyButton value={copyValue} label={`Copy ${label}`}>
            copy
          </CopyButton>
        </div>
      )}
    </SummaryMetric>
  );
}

function EvidenceToken({
  value,
  copyLabel,
}: {
  value: string;
  copyLabel: string;
}) {
  return (
    <StatusBadge tone="neutral" className="gap-1 rounded-md px-2 py-1">
      <span className="font-mono text-[11px] text-ink-dim">{value}</span>
      <CopyButton
        value={value}
        label={copyLabel}
        copiedLabel="copied"
        className="h-6 px-1.5 text-[10px]"
      >
        copy
      </CopyButton>
    </StatusBadge>
  );
}

function ExternalLink({ href, children }: { href: string; children: ReactNode }) {
  if (!isHttpUrl(href)) {
    return (
      <StatusBadge tone="neutral" className="rounded-md px-2.5 py-1.5 text-ink-muted">
        {children}
      </StatusBadge>
    );
  }

  return (
    <ToolbarLink
      href={href}
      external
      size="xs"
    >
      {children}
    </ToolbarLink>
  );
}

function ExternalTextLink({
  href,
  children,
}: {
  href: string;
  children: ReactNode;
}) {
  if (!isHttpUrl(href)) return <>{children}</>;

  return (
    <a
      href={href}
      target="_blank"
      rel="noreferrer"
      className="text-signal-protocol hover:text-signal-protocol focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-signal-protocol"
    >
      {children}
    </a>
  );
}

function fmtDateTime(value: string | null): string {
  if (!value) return "unknown";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "unknown";
  return date.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}
