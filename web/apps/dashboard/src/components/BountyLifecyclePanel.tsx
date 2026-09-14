/**
 * BountyLifecyclePanel — slim orchestrator for the bounty lifecycle surface.
 *
 * The 1187-LOC monolith was split (mandate D) into:
 *  - bountyLifecycleShared.tsx — types + pure summary/format helpers
 *  - EvidenceSummary.tsx        — the "evidence" sub-panel
 *  - ActionsSummary.tsx         — the "actions" sub-panel (guidance + controls)
 *
 * This file keeps the header / overview rendering and the action-runner state,
 * and re-exports the public surface importers + tests depend on (no behavior,
 * network, or data-shape changes).
 */
import { useEffect, useId, useMemo, useState } from "react";
import type { AgentListing, Bounty, BountyStatus, TrialRoom } from "../api";
import {
  SummaryMetric,
  SurfacePanel,
} from "./DashboardChrome";
import { StateBadge } from "./StatusPillAdapters";
import {
  bountyStatusTone,
  formatDateTime,
  skillsFromBountyCard,
  STATUS_ORDER,
  summarizeBountyLifecycle,
  EMPTY_AGENTS,
  EMPTY_TRIAL_ROOMS,
  type BountyLifecycleAction,
  type BountyLifecyclePanelActions,
  type BountyLifecycleView,
} from "./bountyLifecycleShared";
import { EvidenceSummary, SmallStatusPill } from "./EvidenceSummary";
import { ActionsSummary } from "./ActionsSummary";

export { summarizeBountyLifecycle };
export type { BountyLifecycleAction, BountyLifecycleView };

export type BountyLifecyclePanelProps = {
  bounty: Bounty;
  activeView?: BountyLifecycleView;
  me?: string | null;
  myAgents?: readonly AgentListing[];
  trialRooms?: readonly TrialRoom[];
  actions?: BountyLifecyclePanelActions;
  busyAction?: BountyLifecycleAction | null;
  actionError?: string | null;
  className?: string;
};

type LocalActionState = {
  action: BountyLifecycleAction | null;
  error: string | null;
};

export function BountyLifecyclePanel({
  bounty,
  activeView = "overview",
  me = null,
  myAgents = EMPTY_AGENTS,
  trialRooms = EMPTY_TRIAL_ROOMS,
  actions,
  busyAction = null,
  actionError = null,
  className,
}: BountyLifecyclePanelProps) {
  const titleId = useId();
  const claimSelectId = useId();
  const summary = useMemo(
    () => summarizeBountyLifecycle(bounty, {
      me,
      myAgents,
      trialRooms,
    }),
    [bounty, me, myAgents, trialRooms],
  );
  const [selectedAgent, setSelectedAgent] = useState(
    summary.eligibleClaimAgents[0]?.name ?? "",
  );
  const [localAction, setLocalAction] = useState<LocalActionState>({
    action: null,
    error: null,
  });
  const activeAction = busyAction || localAction.action;
  const displayedError = actionError || localAction.error;
  const rootClass = [
    "min-w-0 bg-runtime-bg/60 p-4",
    className || "",
  ]
    .filter(Boolean)
    .join(" ");

  useEffect(() => {
    if (summary.eligibleClaimAgents.some((agent) => agent.name === selectedAgent)) {
      return;
    }
    setSelectedAgent(summary.eligibleClaimAgents[0]?.name ?? "");
  }, [selectedAgent, summary.eligibleClaimAgents]);

  async function runAction(
    action: BountyLifecycleAction,
    callback: (() => void | Promise<void>) | undefined,
  ) {
    if (!callback || activeAction) return;
    setLocalAction({ action, error: null });
    try {
      await callback();
      setLocalAction({ action: null, error: null });
    } catch (ex) {
      setLocalAction({
        action: null,
        error: ex instanceof Error ? ex.message : String(ex),
      });
    }
  }

  return (
    <SurfacePanel as="article" aria-labelledby={titleId} className={rootClass}>
      <div className="flex min-w-0 flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-baseline gap-2">
            <h3
              id={titleId}
              className="min-w-0 max-w-full break-words text-base font-semibold text-ink"
            >
              {bounty.title}
            </h3>
            <BountyStatusPill status={bounty.status} />
            <span className="min-w-0 max-w-full break-all font-mono text-[11px] text-ink-muted">
              {bounty.slug}
            </span>
          </div>
          <p className="mt-2 max-w-4xl whitespace-pre-wrap break-words text-sm leading-relaxed text-ink-soft">
            {bounty.description}
          </p>
          <BountyMeta bounty={bounty} />
        </div>
        <div className="grid min-w-0 grid-cols-2 gap-2 text-xs sm:w-auto sm:min-w-[260px]">
          <SummaryMetric
            label="role"
            value={summary.role}
            tone={summary.role === "owner" ? "amber" : "neutral"}
          />
        </div>
      </div>

      {activeView === "overview" && (
        <div className="mt-4 space-y-4">
          <LifecycleSteps status={bounty.status} />
          <div className="grid min-w-0 gap-2 text-xs sm:grid-cols-2 lg:grid-cols-4">
            <SummaryMetric label="status" value={bounty.status} />
            <SummaryMetric
              label="claimed agent"
              value={bounty.claimed_agent_name || "-"}
            />
            <SummaryMetric
              label="linked trials"
              value={summary.evidence.linkedTrials}
            />
            <SummaryMetric
              label="best score"
              value={
                summary.evidence.bestScore === null
                  ? "-"
                  : `${summary.evidence.bestScore}/100`
              }
              tone={
                summary.evidence.bestScore !== null &&
                summary.evidence.bestScore >= 70
                  ? "emerald"
                  : "neutral"
              }
            />
          </div>
          <ClaimedAgentPanel bounty={bounty} />
        </div>
      )}

      {activeView === "evidence" && (
        <EvidenceSummary bounty={bounty} summary={summary} />
      )}

      {activeView === "actions" && (
        <ActionsSummary
          bounty={bounty}
          summary={summary}
          actions={actions}
          activeAction={activeAction}
          selectedAgent={selectedAgent}
          setSelectedAgent={setSelectedAgent}
          claimSelectId={claimSelectId}
          runAction={runAction}
          displayedError={displayedError}
        />
      )}
    </SurfacePanel>
  );
}

function BountyMeta({ bounty }: { bounty: Bounty }) {
  return (
    <div className="mt-3 flex min-w-0 flex-col gap-2 text-[11px] text-ink-muted">
      <div className="flex flex-wrap gap-x-4 gap-y-1">
        {bounty.posted_by_email && (
          <span className="min-w-0 break-words">
            posted by{" "}
            <span className="break-all font-mono text-ink-soft">
              {bounty.posted_by_email}
            </span>
          </span>
        )}
        {bounty.claimed_at && (
          <span className="min-w-0 break-words">
            claimed {formatDateTime(bounty.claimed_at)}
          </span>
        )}
        {bounty.fulfilled_at && (
          <span className="min-w-0 break-words">
            fulfilled {formatDateTime(bounty.fulfilled_at)}
          </span>
        )}
      </div>
      {bounty.tags.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {bounty.tags.map((tag) => (
            <span
              key={tag}
              className="max-w-full break-all rounded-md bg-runtime-panel px-1.5 py-0.5 text-[11px] text-ink-dim"
            >
              {tag}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

function LifecycleSteps({ status }: { status: BountyStatus }) {
  const isCancelled = status === "cancelled";
  const activeIndex = status === "cancelled" ? 3 : STATUS_ORDER.indexOf(status);
  const steps: readonly { status: BountyStatus; label: string }[] = isCancelled
    ? [
        { status: "open", label: "Posted" },
        { status: "claimed", label: "Claim review" },
        { status: "fulfilled", label: "Fulfillment" },
        { status: "cancelled", label: "Cancelled" },
      ]
    : [
        { status: "open", label: "Posted" },
        { status: "claimed", label: "Claimed" },
        { status: "fulfilled", label: "Fulfilled" },
      ];

  return (
    <ol
      className="mt-4 grid gap-2 text-xs sm:grid-cols-3 lg:grid-cols-4"
      aria-label="Bounty lifecycle"
    >
      {steps.map((step, index) => {
        const current = step.status === status;
        const complete = index < activeIndex && !isCancelled;
        return (
          <li
            key={step.status}
            aria-current={current ? "step" : undefined}
            className={[
              "min-w-0 rounded-md border px-3 py-2",
              current || complete
                ? current
                  ? "border-signal-authority/45 bg-signal-authority/12"
                  : "border-signal-live/45 bg-signal-live/12"
                : "border-runtime-line-soft/60 bg-runtime-panel/30",
            ].join(" ")}
          >
            <div className="flex min-w-0 items-center gap-2">
              <span
                aria-hidden="true"
                className={[
                  "h-2 w-2 shrink-0 rounded-full",
                  current
                    ? "bg-signal-authority"
                    : complete
                      ? "bg-signal-live"
                      : "bg-runtime-line",
                ].join(" ")}
              />
              <span
                className={[
                  "truncate",
                  current || complete ? "text-ink" : "text-ink-muted",
                ].join(" ")}
              >
                {step.label}
              </span>
            </div>
          </li>
        );
      })}
    </ol>
  );
}

function ClaimedAgentPanel({ bounty }: { bounty: Bounty }) {
  if (!bounty.claimed_agent_name) {
    return (
      <SurfacePanel as="section" className="min-w-0 p-3">
        <h4 className="text-xs font-medium uppercase text-ink-muted">
          Claimed agent
        </h4>
        <p className="mt-2 text-xs text-ink-muted">
          No agent has claimed this bounty.
        </p>
      </SurfacePanel>
    );
  }

  const skills = skillsFromBountyCard(bounty.claimed_agent_card);

  return (
    <SurfacePanel
      as="section"
      className="min-w-0 border-signal-authority/45 bg-signal-authority/12 p-3"
    >
      <div className="flex min-w-0 flex-wrap items-center gap-2">
        <h4 className="truncate text-xs font-medium uppercase text-signal-authority">
          Claimed agent
        </h4>
        <SmallStatusPill status={bounty.claimed_agent_status || "unknown"} />
      </div>
      <div className="mt-2 break-all font-mono text-sm text-ink">
        {bounty.claimed_agent_name}
      </div>
      <div className="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-ink-muted">
        {bounty.claimed_agent_version && (
          <span>v{bounty.claimed_agent_version}</span>
        )}
        {bounty.claimed_agent_url && (
          <a
            href={bounty.claimed_agent_url}
            target="_blank"
            rel="noreferrer"
            className="break-all rounded-md text-signal-authority hover:text-signal-authority focus:outline-none focus-visible:ring-2 focus-visible:ring-signal-authority/40"
          >
            Agent URL
          </a>
        )}
      </div>
      {skills.length > 0 && (
        <div className="mt-3 flex flex-wrap gap-1.5">
          {skills.slice(0, 6).map((skill) => (
            <span
              key={skill}
              className="max-w-full break-all rounded-md bg-runtime-bg px-1.5 py-0.5 font-mono text-[11px] text-ink-soft"
            >
              {skill}
            </span>
          ))}
          {skills.length > 6 && (
            <span className="text-[11px] text-signal-authority">
              +{skills.length - 6} more
            </span>
          )}
        </div>
      )}
    </SurfacePanel>
  );
}

function BountyStatusPill({ status }: { status: BountyStatus }) {
  return (
    <StateBadge
      status={status}
      tone={bountyStatusTone(status)}
      dot={status === "open" || status === "claimed"}
    />
  );
}
