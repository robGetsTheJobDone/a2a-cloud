import { useId, type ReactNode } from "react";
import {
  SurfacePanel,
  ToolbarLink,
} from "./DashboardChrome";
import { StatusBadge } from "./StatusPillAdapters";

type AgentLifecycleStateTone =
  | "neutral"
  | "amber"
  | "red"
  | "emerald";

type AgentLifecycleStateSize = "compact" | "comfortable";

type AgentLifecycleEmptyStateKind =
  | "no-agents"
  | "no-proofs"
  | "no-deployments"
  | "no-secrets"
  | "no-insights"
  | "filtered-empty";

type AgentLifecycleEmptyStateCopy = {
  title: string;
  description: string;
  tone: AgentLifecycleStateTone;
  details?: readonly string[];
};

type AgentLifecycleStateProps = {
  title: ReactNode;
  description?: ReactNode;
  details?: readonly ReactNode[];
  action?: ReactNode;
  tone?: AgentLifecycleStateTone;
  size?: AgentLifecycleStateSize;
  role?: "status" | "alert" | "note";
  className?: string;
};

type AgentLifecyclePresetEmptyStateProps = {
  kind: AgentLifecycleEmptyStateKind;
  title?: ReactNode;
  description?: ReactNode;
  details?: readonly ReactNode[];
  action?: ReactNode;
  size?: AgentLifecycleStateSize;
  className?: string;
};

export type AgentLifecycleLoadingStateProps = {
  label?: string;
  detail?: string;
  rows?: number;
  className?: string;
};

const AGENT_LIFECYCLE_EMPTY_STATE_COPY: Record<
  AgentLifecycleEmptyStateKind,
  AgentLifecycleEmptyStateCopy
> = {
  "no-agents": {
    title: "No agents yet",
    description:
      "Describe what you want in Studio and a crew builds, reviews, repairs, and ships it — with a live URL and a signed proof receipt. Already run an A2A endpoint? Import it instead.",
    tone: "neutral",
    details: [
      "Lifecycle status appears after the first agent record is available.",
      "Deployment and proof history are added as the agent runs.",
    ],
  },
  "no-proofs": {
    title: "No proof run yet",
    // Only what /v1/me/agent-proofs/{agent}/run actually returns: a pass/fail
    // row with the events, file ops and result it recorded. That path does not
    // go through the sealing gateway, so it produces no signed receipt and
    // nothing here may promise one.
    description:
      "Pick one of this agent's tools, keep the sample arguments, and run it once. The call is recorded as a pass or fail with the events it emitted, the files it touched, and the result it returned.",
    tone: "neutral",
    details: [
      "A passing run flips this agent's badge to verified; without one it stays unverified.",
      "Failed runs stay visible so the next attempt has useful context.",
    ],
  },
  "no-deployments": {
    title: "No deployments recorded",
    description:
      "The next source deploy or runtime update will create a deployment timeline here.",
    tone: "neutral",
    details: [
      "Build, reconcile, runtime, and verification events are shown together.",
      "Failed deployment rows are retained so cleanup can be retried.",
    ],
  },
  "no-secrets": {
    title: "No secrets configured",
    description:
      "Add environment keys for values that should be projected into this agent runtime.",
    tone: "neutral",
    details: [
      "Saved values are masked after storage.",
      "Reserved platform keys should stay managed by the runtime.",
    ],
  },
  "no-insights": {
    title: "No call insights",
    description:
      "Invocation logs and latency details appear after this agent receives traffic.",
    tone: "neutral",
    details: [
      "Recent calls, latency, and failures will appear here after traffic arrives.",
      "If traffic is expected, confirm the agent URL and runtime status.",
    ],
  },
  "filtered-empty": {
    title: "No lifecycle matches",
    description:
      "Adjust search, status, visibility, proof, or runtime filters to broaden the result set.",
    tone: "amber",
    details: [
      "Clear filters before treating this as an empty account.",
      "Return to the full lifecycle list before changing the agent setup.",
    ],
  },
};

const sizeClasses: Record<AgentLifecycleStateSize, string> = {
  compact: "p-3",
  comfortable: "p-4",
};

function AgentLifecycleState({
  title,
  description,
  details,
  action,
  tone = "neutral",
  size = "compact",
  role = "note",
  className,
}: AgentLifecycleStateProps) {
  const titleId = useId();
  const descriptionId = useId();
  const detailId = useId();
  const detailItems = details ?? [];
  const hasDescription = Boolean(description);
  const hasDetails = detailItems.length > 0;

  return (
    <SurfacePanel
      as="section"
      role={role}
      aria-labelledby={titleId}
      aria-describedby={describedBy({
        descriptionId: hasDescription ? descriptionId : null,
        detailId: hasDetails ? detailId : null,
      })}
      className={classes("bg-runtime-bg/70", sizeClasses[size], className)}
    >
      <div className="flex min-w-0 items-start gap-3">
        <StatusBadge tone={tone} dot className="mt-0.5 shrink-0 px-1.5">
          <span className="sr-only">{tone}</span>
        </StatusBadge>
        <div className="min-w-0 flex-1">
          <h2 id={titleId} className="text-sm font-semibold text-ink">
            {title}
          </h2>
          {description && (
            <p
              id={descriptionId}
              className="mt-1 max-w-3xl text-xs leading-relaxed text-ink-dim"
            >
              {description}
            </p>
          )}
          {hasDetails && (
            <ul
              id={detailId}
              className="mt-2 grid gap-1 text-[11px] leading-relaxed text-ink-muted sm:grid-cols-2"
            >
              {detailItems.map((item, index) => (
                <li key={index} className="flex min-w-0 gap-1.5">
                  <span aria-hidden="true" className="shrink-0">
                    -
                  </span>
                  <span className="min-w-0">{item}</span>
                </li>
              ))}
            </ul>
          )}
        </div>
        {action && <div className="shrink-0">{action}</div>}
      </div>
    </SurfacePanel>
  );
}

function AgentLifecycleEmptyState({
  kind,
  title,
  description,
  details,
  action,
  size,
  className,
}: AgentLifecyclePresetEmptyStateProps) {
  const copy = AGENT_LIFECYCLE_EMPTY_STATE_COPY[kind];

  return (
    <AgentLifecycleState
      title={title ?? copy.title}
      description={description ?? copy.description}
      details={details ?? copy.details}
      action={action}
      tone={copy.tone}
      size={size}
      role="status"
      className={className}
    />
  );
}

/**
 * The zero-agent state is the first thing a new account sees on /my-agents, so
 * it ships its own way out by default: Studio for "build one", import for an
 * endpoint that already exists. Callers can still pass their own `action`.
 */
export function NoAgentsYetState(props: OmitPresetKindProps) {
  return (
    <AgentLifecycleEmptyState
      {...props}
      kind="no-agents"
      action={props.action ?? <NoAgentsYetActions />}
    />
  );
}

function NoAgentsYetActions() {
  return (
    <div className="flex flex-col gap-2 sm:flex-row">
      <ToolbarLink href="/studio" variant="primary">
        Describe an agent
      </ToolbarLink>
      <ToolbarLink href="/my-agents/import">
        Import an existing endpoint
      </ToolbarLink>
    </div>
  );
}

/**
 * "No proof run yet" is only useful if it ships the way out. Its one caller
 * stands in the proof workbench and passes the run button itself as `action`,
 * so there is no default here — a fallback nobody reaches would just be an
 * affordance that never renders.
 */
export function NoAgentProofsState(props: OmitPresetKindProps) {
  return <AgentLifecycleEmptyState {...props} kind="no-proofs" />;
}

export function FilteredAgentLifecycleEmptyState(props: OmitPresetKindProps) {
  return <AgentLifecycleEmptyState {...props} kind="filtered-empty" />;
}

export function AgentLifecycleLoadingState({
  label = "Loading agent lifecycle...",
  detail = "Preparing the lifecycle view.",
  rows = 3,
  className,
}: AgentLifecycleLoadingStateProps) {
  const titleId = useId();
  const detailId = useId();
  const rowCount = clampedRows(rows);

  return (
    <SurfacePanel
      as="section"
      role="status"
      aria-live="polite"
      aria-labelledby={titleId}
      aria-describedby={detailId}
      className={classes("bg-runtime-bg/70 p-3", className)}
    >
      <div className="flex items-center justify-between gap-3">
        <div className="min-w-0">
          <h2 id={titleId} className="text-sm font-semibold text-ink-soft">
            {label}
          </h2>
          <p id={detailId} className="mt-1 text-xs leading-relaxed text-ink-muted">
            {detail}
          </p>
        </div>
        <span
          aria-hidden="true"
          className="h-2 w-2 shrink-0 rounded-full bg-ink-muted"
        />
      </div>
      <div className="mt-3 grid gap-2" aria-hidden="true">
        {Array.from({ length: rowCount }, (_, index) => (
          <SurfacePanel
            as="div"
            key={index}
            className="grid gap-2 bg-runtime-panel/30 p-2 sm:grid-cols-[minmax(120px,0.8fr)_minmax(180px,1fr)_80px]"
          >
            <div className="h-2 rounded-md bg-runtime-raised" />
            <div className="h-2 rounded-md bg-runtime-raised/80" />
            <div className="h-2 rounded-md bg-runtime-raised/60" />
          </SurfacePanel>
        ))}
      </div>
    </SurfacePanel>
  );
}

type OmitPresetKindProps = Omit<AgentLifecyclePresetEmptyStateProps, "kind">;

function describedBy({
  descriptionId,
  detailId,
}: {
  descriptionId: string | null;
  detailId: string | null;
}) {
  return [descriptionId, detailId].filter(Boolean).join(" ") || undefined;
}

function clampedRows(rows: number) {
  if (!Number.isFinite(rows)) return 3;
  return Math.max(1, Math.min(6, Math.floor(rows)));
}

function classes(...items: Array<string | false | null | undefined>) {
  return items.filter(Boolean).join(" ");
}
