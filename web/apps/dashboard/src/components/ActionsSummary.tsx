/**
 * ActionsSummary — the "Actions" sub-panel of the bounty lifecycle surface
 * (mandate D: split the former 1187-LOC BountyLifecyclePanel into focused
 * sub-panels). Renders lifecycle guidance plus the claim / trial / fulfill /
 * cancel action controls. Action callbacks + busy/error state are owned by the
 * orchestrating BountyLifecyclePanel and passed in.
 */
import {
  InlineAlert,
  SelectInput,
  SurfacePanel,
  ToolbarButton,
} from "./DashboardChrome";
import type { Bounty } from "../api";
import {
  GUIDANCE_TONES,
  type BountyLifecycleAction,
  type BountyLifecycleGuidance,
  type BountyLifecyclePanelActions,
  type BountyLifecycleSummary,
} from "./bountyLifecycleShared";

export function ActionsSummary({
  bounty,
  summary,
  actions,
  activeAction,
  selectedAgent,
  setSelectedAgent,
  claimSelectId,
  runAction,
  displayedError,
}: {
  bounty: Bounty;
  summary: BountyLifecycleSummary;
  actions: BountyLifecyclePanelActions;
  activeAction: BountyLifecycleAction | null;
  selectedAgent: string;
  setSelectedAgent: (value: string) => void;
  claimSelectId: string;
  runAction: (
    action: BountyLifecycleAction,
    callback: (() => void | Promise<void>) | undefined,
  ) => void;
  displayedError: string | null;
}) {
  return (
    <aside className="mt-4 min-w-0 space-y-4" aria-label="Bounty lifecycle guidance">
      <GuidancePanel guidance={summary.guidance} />
      <ActionPanel
        bounty={bounty}
        summary={summary}
        actions={actions}
        activeAction={activeAction}
        selectedAgent={selectedAgent}
        setSelectedAgent={setSelectedAgent}
        claimSelectId={claimSelectId}
        runAction={runAction}
      />
      {displayedError && (
        <InlineAlert tone="red" role="alert">
          <span className="break-words">{displayedError}</span>
        </InlineAlert>
      )}
    </aside>
  );
}

function GuidancePanel({
  guidance,
}: {
  guidance: readonly BountyLifecycleGuidance[];
}) {
  return (
    <SurfacePanel as="section" className="min-w-0 p-3">
      <h4 className="text-xs font-medium uppercase text-ink-muted">
        Guidance
      </h4>
      <ul className="mt-3 space-y-2">
        {guidance.map((item) => {
          const tone = GUIDANCE_TONES[item.tone];
          return (
            <li
              key={item.id}
              className={`min-w-0 rounded-md border px-3 py-2 ${tone.border} ${tone.bg}`}
            >
              <div className="flex min-w-0 items-start gap-2">
                <span
                  aria-hidden="true"
                  className={`mt-1 h-2 w-2 shrink-0 rounded-full ${tone.marker}`}
                />
                <div className="min-w-0">
                  <div className={`break-words text-xs font-medium ${tone.text}`}>
                    {item.title}
                  </div>
                  <p className="mt-1 break-words text-xs leading-relaxed text-ink-dim">
                    {item.detail}
                  </p>
                </div>
              </div>
            </li>
          );
        })}
      </ul>
    </SurfacePanel>
  );
}

function ActionPanel({
  bounty,
  summary,
  actions,
  activeAction,
  selectedAgent,
  setSelectedAgent,
  claimSelectId,
  runAction,
}: {
  bounty: Bounty;
  summary: BountyLifecycleSummary;
  actions: BountyLifecyclePanelActions;
  activeAction: BountyLifecycleAction | null;
  selectedAgent: string;
  setSelectedAgent: (value: string) => void;
  claimSelectId: string;
  runAction: (
    action: BountyLifecycleAction,
    callback: (() => void | Promise<void>) | undefined,
  ) => void;
}) {
  const hasAnyAction = Boolean(
    actions?.onCreateTrial ||
      actions?.onClaim ||
      actions?.onFulfill ||
      actions?.onCancel,
  );

  if (!hasAnyAction) return null;



  return (
    <SurfacePanel
      as="section"
      className="min-w-0 p-3"
      aria-label={`Actions for ${bounty.title}`}
    >
      <h4 className="text-xs font-medium uppercase text-ink-muted">Actions</h4>
      <div className="mt-3 flex min-w-0 flex-col gap-2">
        {actions?.onCreateTrial && (
          <ToolbarButton
            type="button"
            disabled={Boolean(activeAction)}
            aria-busy={activeAction === "create-trial"}
            onClick={() =>
              runAction("create-trial", () =>
                actions.onCreateTrial?.(summary.trialDraft, bounty),
              )
            }
            variant="primary"
            className="w-full sm:w-auto"
          >
            {activeAction === "create-trial"
              ? "Creating trial..."
              : summary.evidence.linkedTrials > 0
                ? "Create another trial"
                : "Create trial"}
          </ToolbarButton>
        )}

        {actions?.onClaim && summary.claimable && (
          <div
            role="group"
            aria-label={`Claim ${bounty.title}`}
            className="flex min-w-0 flex-col gap-2 sm:flex-row sm:items-center"
          >
            <label htmlFor={claimSelectId} className="sr-only">
              Agent to claim {bounty.title}
            </label>
            <SelectInput
              id={claimSelectId}
              value={selectedAgent}
              onChange={(event) => setSelectedAgent(event.target.value)}
              disabled={Boolean(activeAction)}
              className="min-w-0 text-xs sm:w-56"
            >
              {summary.eligibleClaimAgents.map((agent) => (
                <option key={agent.name} value={agent.name}>
                  {agent.name}
                </option>
              ))}
            </SelectInput>
            <ToolbarButton
              type="button"
              disabled={Boolean(activeAction) || !selectedAgent}
              aria-busy={activeAction === "claim"}
              onClick={() =>
                runAction("claim", () =>
                  selectedAgent
                    ? actions.onClaim?.(selectedAgent, bounty)
                    : undefined,
                )
              }
              variant="primary"
              className="w-full sm:w-auto"
            >
              {activeAction === "claim" ? "Claiming..." : "Claim with agent"}
            </ToolbarButton>
          </div>
        )}

        {actions?.onFulfill && summary.canFulfill && (
          <ToolbarButton
            type="button"
            disabled={Boolean(activeAction)}
            aria-busy={activeAction === "fulfill"}
            onClick={() =>
              runAction("fulfill", () => actions.onFulfill?.(bounty))
            }
            variant="primary"
            className="w-full sm:w-auto"
          >
            {activeAction === "fulfill" ? "Marking fulfilled..." : "Mark fulfilled"}
          </ToolbarButton>
        )}

        {actions?.onCancel && summary.canCancel && (
          <ToolbarButton
            type="button"
            disabled={Boolean(activeAction)}
            aria-busy={activeAction === "cancel"}
            onClick={() => runAction("cancel", () => actions.onCancel?.(bounty))}
            variant="danger"
            className="w-full sm:w-auto"
          >
            {activeAction === "cancel" ? "Cancelling..." : "Cancel"}
          </ToolbarButton>
        )}
      </div>
    </SurfacePanel>
  );
}
