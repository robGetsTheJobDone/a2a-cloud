/**
 * Bounties tab. Three responsibilities:
 *
 * 1. Browse open bounties anyone can claim.
 * 2. Manage bounties I posted (cancel, fulfill).
 * 3. Claim an open bounty with one of my deployed agents.
 *
 * Request workflow surface: open requests, mine, claimed, fulfilled.
 */
import { useCallback, useId, useMemo, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import {
  cancelBounty,
  claimBounty,
  createTrialRoom,
  fulfillBounty,
  listAgents,
  listBounties,
  listTrialRooms,
  session,
  type AgentListing,
  type Bounty,
  type TrialRoom,
} from "../api";
import {
  BountyLifecyclePanel,
  type BountyLifecycleView,
  summarizeBountyLifecycle,
} from "./BountyLifecyclePanel";
import { NewBountySheet } from "./NewBountySheet";
import {
  EmptyState,
  InlineAlert,
  LiveRegion,
  LoadingState,
  SegmentedControl,
  SummaryMetric,
  SurfacePanel,
  TabLink,
  ToolbarButton,
  ToolbarLink,
  type StatusBadgeTone,
} from "./DashboardChrome";
import { StateBadge } from "./StatusPillAdapters";
import {
  DashboardSurfacePosture,
  type SurfacePostureMetric,
  type SurfacePostureStatus,
} from "./SurfacePosture";
import { DetailSheet } from "./ListDetailLayout";
import {
  DASHBOARD_SECTION_CACHE_KEYS,
  useDashboardSectionResource,
} from "./DashboardSectionCache";
import {
  BOUNTY_VIEWS,
  normalizeBountyViewId,
  type BountyViewId,
} from "../navigation";
import { RoutePageShell } from "./RoutePageShell";

type BountiesResource = {
  bounties: Bounty[];
  agents: AgentListing[];
  trialRooms: TrialRoom[];
};

export function Bounties() {
  const me = session.load()?.email ?? "";
  const resultsId = useId();
  const navigate = useNavigate();
  const { view, bountySlug, section } = useParams<{
    view?: string;
    bountySlug?: string;
    section?: string;
  }>();
  const creating = view === "new";
  const filter = creating ? "open" : normalizeBountyViewId(view);
  const detailView = normalizeBountyDetailView(section);
  const loadBountiesResource = useCallback(async (): Promise<BountiesResource> => {
    const [bounties, agents, trialRooms] = await Promise.all([
      listBounties(),
      listAgents(),
      listTrialRooms({ limit: 100 }),
    ]);
    return { bounties, agents, trialRooms };
  }, []);
  const {
    data,
    error: loadErr,
    loading,
    refresh: refreshBounties,
  } = useDashboardSectionResource(
    DASHBOARD_SECTION_CACHE_KEYS.agents.bounties,
    loadBountiesResource,
  );
  const bounties = data?.bounties ?? null;
  const agents = data?.agents ?? [];
  const trialRooms = data?.trialRooms ?? [];
  const [actionErr, setActionErr] = useState<string | null>(null);
  const err = actionErr ?? loadErr;

  const refresh = useCallback(async () => {
    setActionErr(null);
    try {
      await refreshBounties();
    } catch {
      // The section cache stores and exposes the load error.
    }
  }, [refreshBounties]);

  const visible = useMemo(() => {
    if (!bounties) return [];
    switch (filter) {
      case "open":
        return bounties.filter((b) => b.status === "open");
      case "mine":
        return bounties.filter((b) => b.posted_by_email === me);
      case "claimed":
        return bounties.filter((b) => b.status === "claimed");
      case "fulfilled":
        return bounties.filter((b) => b.status === "fulfilled");
    }
  }, [bounties, filter, me]);
  const selectedBounty = useMemo(() => {
    if (!bounties || !bountySlug) return null;
    const decodedSlug = decodeURIComponent(bountySlug);
    return bounties.find((bounty) => bounty.slug === decodedSlug) || null;
  }, [bounties, bountySlug]);
  const marketSummary = useMemo(
    () => summarizeBountyMarket(bounties, { me, myAgents: agents, trialRooms }),
    [agents, bounties, me, trialRooms],
  );
  const selectedFilterLabel =
    BOUNTY_VIEWS.find((f) => f.id === filter)?.label ?? "selected";
  const liveSummary =
    bounties === null && loading
      ? "Loading bounties."
      : err
        ? `Bounties error: ${err}`
        : creating
          ? "Posting a new bounty."
        : bounties === null
          ? "Bounties are not loaded."
          : selectedBounty
            ? `Viewing bounty ${selectedBounty.title}.`
            : visible.length === 0
            ? `No bounties in the ${selectedFilterLabel} view.`
            : `${visible.length} ${visible.length === 1 ? "bounty" : "bounties"} in the ${selectedFilterLabel} view.`;
  const filterPath = BOUNTY_VIEWS.find((item) => item.id === filter)?.path || "/bounties";
  // Mandate C: detail + new-bounty are URL-addressable but open as in-place
  // right-side sheets layered over the list. The list NEVER unmounts.
  const detailOpen = Boolean(selectedBounty);
  const notFoundOpen = Boolean(bountySlug) && !selectedBounty && bounties !== null;
  const closeDetail = useCallback(() => navigate(filterPath), [navigate, filterPath]);

  return (
    <RoutePageShell
      routeId="bounties"
      actions={
        <ToolbarLink
          href="/bounties/new"
          variant="primary"
          size="md"
        >
          Post a bounty
        </ToolbarLink>
      }
    >
      <LiveRegion politeness={err ? "assertive" : "polite"}>
        {liveSummary}
      </LiveRegion>

      <BountyMarketPosture
        bounties={bounties}
        summary={marketSummary}
        visibleCount={visible.length}
        activeFilter={filter}
        selectedBounty={selectedBounty}
        creating={creating}
        loading={loading}
        me={me}
      />

      {err && (
        <InlineAlert tone="red" role="alert">
          <span className="break-words">{err}</span>
        </InlineAlert>
      )}

      <section
        id={resultsId}
        aria-label={`${selectedFilterLabel} bounties`}
        aria-busy={loading}
        className="min-w-0"
      >
        {bounties === null && loading ? (
          <div role="status" aria-live="polite">
            <LoadingState label="Loading bounties..." />
          </div>
        ) : bounties === null ? (
          <EmptyState
            title="Bounties could not load"
            description="Check the error above and retry the request."
            action={
              <ToolbarButton
                variant="secondary"
                onClick={refresh}
                disabled={loading}
              >
                Retry
              </ToolbarButton>
            }
          />
        ) : visible.length === 0 ? (
          <div role="status" aria-live="polite">
            <EmptyState
              title="No bounties match this view"
              description="Post a new request or switch filters to see claimed and fulfilled work."
              action={
                <ToolbarLink
                  href="/bounties/new"
                  variant="primary"
                >
                  Post the first bounty
                </ToolbarLink>
              }
            />
          </div>
        ) : (
          <ul
            className="space-y-2"
            aria-label={`${selectedFilterLabel} bounties`}
          >
            {visible.map((b) => (
              <li key={b.slug}>
                <BountySummaryRow
                  bounty={b}
                  me={me}
                  myAgents={agents}
                  trialRooms={trialRooms}
                  href={bountyDetailPath(filter, b.slug)}
                />
              </li>
            ))}
          </ul>
        )}
      </section>

      {/* Mandate C: post-a-bounty opens in place as its OWN right-side sheet,
          distinct from the detail sheet below. The list stays mounted. */}
      <NewBountySheet
        open={creating}
        onClose={() => navigate(filterPath)}
        onCreated={async () => {
          await refresh();
        }}
      />

      {/* Mandate C: request detail opens in place as a right-side sheet. */}
      <DetailSheet
        open={detailOpen}
        onClose={closeDetail}
        title={selectedBounty ? selectedBounty.title : "Bounty"}
        description={selectedBounty ? selectedBounty.slug : undefined}
        size="xl"
      >
        {selectedBounty && (
          <BountyDetailView
            bounty={selectedBounty}
            filter={filter}
            activeView={detailView}
            me={me}
            myAgents={agents}
            trialRooms={trialRooms}
            onChange={refresh}
          />
        )}
      </DetailSheet>

      {/* Stale/missing slug: surface as a sheet, list stays mounted behind. */}
      <DetailSheet
        open={notFoundOpen}
        onClose={closeDetail}
        title="Bounty not found"
        description="This request is no longer available or the link is stale."
        size="sm"
      >
        <EmptyState
          title="Bounty not found"
          description="This request is no longer available or the link is stale."
          action={<ToolbarLink href={filterPath}>Back to bounties</ToolbarLink>}
        />
      </DetailSheet>
    </RoutePageShell>
  );
}

const BOUNTY_DETAIL_VIEWS: Array<{
  id: BountyLifecycleView;
  label: string;
  description: string;
}> = [
  {
    id: "overview",
    label: "Overview",
    description: "Request brief, lifecycle state, and ownership context.",
  },
  {
    id: "evidence",
    label: "Evidence",
    description: "Linked trials, claimed-agent runs, scores, receipts, and file changes.",
  },
  {
    id: "actions",
    label: "Actions",
    description: "Claim, create trial evidence, fulfill, cancel, and review lifecycle guidance.",
  },
];

type BountyMarketSummary = {
  total: number;
  open: number;
  mine: number;
  claimed: number;
  fulfilled: number;
  cancelled: number;
  claimable: number;
  readyToFulfill: number;
  needsEvidence: number;
  linkedTrials: number;
  receipts: number;
  firstOpen: Bounty | null;
  firstClaimable: Bounty | null;
  firstNeedsEvidence: Bounty | null;
  firstReadyToFulfill: Bounty | null;
};

type BountyMarketNextAction = {
  label: string;
  detail: string;
  href: string;
  action: string;
  variant: "primary" | "secondary";
};

function BountyMarketPosture({
  bounties,
  summary,
  visibleCount,
  activeFilter,
  selectedBounty,
  creating,
  loading,
  me,
}: {
  bounties: Bounty[] | null;
  summary: BountyMarketSummary;
  visibleCount: number;
  activeFilter: BountyViewId;
  selectedBounty: Bounty | null;
  creating: boolean;
  loading: boolean;
  me: string;
}) {
  const nextAction = bountyMarketNextAction({
    bounties,
    summary,
    activeFilter,
    selectedBounty,
    creating,
    me,
  });
  const tone = bountyMarketPostureTone(bounties, summary);
  const label = bountyMarketPostureLabel(bounties, summary);
  const detail = bountyMarketPostureDetail({
    bounties,
    summary,
    visibleCount,
    activeFilter,
    selectedBounty,
    creating,
    loading,
  });

  const status: SurfacePostureStatus = {
    label,
    tone: postureStatusPillTone(tone),
    dot: tone === "emerald" || tone === "amber",
  };
  const metrics: SurfacePostureMetric[] = [
    {
      label: "visible",
      value: bounties ? visibleCount.toLocaleString() : "loading",
    },
    {
      label: "open",
      value: summary.open.toLocaleString(),
      tone: summary.open > 0 ? "authority" : "neutral",
    },
    {
      label: "mine",
      value: summary.mine.toLocaleString(),
      tone: summary.readyToFulfill > 0 ? "live" : "neutral",
    },
    {
      label: "claimed",
      value: summary.claimed.toLocaleString(),
      tone: summary.needsEvidence > 0 ? "authority" : "neutral",
    },
    {
      label: "evidence",
      value: summary.linkedTrials.toLocaleString(),
      tone: summary.receipts > 0 ? "live" : "neutral",
    },
  ];

  return (
    <DashboardSurfacePosture
      data-onboarding-target="bounties-posture"
      eyebrow={detail}
      title={nextAction.label}
      status={status}
      metrics={metrics}
      actions={
        <>
          {!creating && (
            <ToolbarLink href="/bounties/new" size="sm">
              Post a bounty
            </ToolbarLink>
          )}
          <ToolbarLink href={nextAction.href} variant={nextAction.variant} size="sm">
            {nextAction.action}
          </ToolbarLink>
        </>
      }
    />
  );
}

function postureStatusPillTone(
  tone: StatusBadgeTone,
): SurfacePostureStatus["tone"] {
  switch (tone) {
    case "emerald":
      return "live";
    case "amber":
      return "authority";
    case "red":
      return "danger";
    default:
      return "neutral";
  }
}

function normalizeBountyDetailView(value: string | undefined): BountyLifecycleView {
  return BOUNTY_DETAIL_VIEWS.some((item) => item.id === value)
    ? (value as BountyLifecycleView)
    : "overview";
}

function BountyDetailView({
  bounty,
  filter,
  activeView,
  me,
  myAgents,
  trialRooms,
  onChange,
}: {
  bounty: Bounty;
  filter: BountyViewId;
  activeView: BountyLifecycleView;
  me: string;
  myAgents: AgentListing[];
  trialRooms: TrialRoom[];
  onChange: () => void;
}) {
  const activeDetail =
    BOUNTY_DETAIL_VIEWS.find((item) => item.id === activeView) ||
    BOUNTY_DETAIL_VIEWS[0];
  return (
    <div className="space-y-3">
      <div className="space-y-1.5">
        <SegmentedControl
          role="tablist"
          aria-label={`${bounty.title} detail views`}
          className="flex flex-wrap gap-1"
        >
          {BOUNTY_DETAIL_VIEWS.map((item) => (
            <TabLink
              key={item.id}
              href={bountyDetailPath(filter, bounty.slug, item.id)}
              selected={activeView === item.id}
            >
              {item.label}
            </TabLink>
          ))}
        </SegmentedControl>
        <div className="text-xs leading-relaxed text-ink-muted">
          {activeDetail.description}
        </div>
      </div>
      <BountyLifecyclePanel
        bounty={bounty}
        activeView={activeView}
        me={me}
        myAgents={myAgents}
        trialRooms={trialRooms}
        actions={{
          onCreateTrial: async (draft) => {
            await createTrialRoom(draft);
            onChange();
          },
          onClaim: async (agentName) => {
            await claimBounty(bounty.slug, agentName);
            onChange();
          },
          onFulfill: async () => {
            if (!confirm("Mark this bounty fulfilled?")) return;
            await fulfillBounty(bounty.slug);
            onChange();
          },
          onCancel: async () => {
            if (!confirm("Cancel this bounty? This is terminal.")) return;
            await cancelBounty(bounty.slug);
            onChange();
          },
        }}
      />
    </div>
  );
}

function BountySummaryRow({
  bounty,
  me,
  myAgents,
  trialRooms,
  href,
}: {
  bounty: Bounty;
  me: string;
  myAgents: AgentListing[];
  trialRooms: TrialRoom[];
  href: string;
}) {
  const summary = summarizeBountyLifecycle(bounty, { me, myAgents, trialRooms });
  const tagPreview = bounty.tags.slice(0, 4);

  return (
    <SurfacePanel as="article" className="bg-runtime-bg p-4">
      <div className="grid min-w-0 gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(180px,240px)_auto] lg:items-start">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <StateBadge status={bounty.status} dot={bounty.status === "open" || bounty.status === "claimed"} />
            <span className="break-all font-mono text-[11px] text-ink-faint">
              {bounty.slug}
            </span>
          </div>
          <h2 className="mt-2 break-words text-base font-semibold text-ink">
            {bounty.title}
          </h2>
          <p className="mt-2 line-clamp-2 text-sm leading-relaxed text-ink-dim">
            {bounty.description}
          </p>
          {tagPreview.length > 0 && (
            <div className="mt-3 flex flex-wrap gap-1.5">
              {tagPreview.map((tag) => (
                <span
                  key={tag}
                  className="rounded-md border border-runtime-line-soft/60 px-1.5 py-0.5 text-[11px] text-ink-muted"
                >
                  {tag}
                </span>
              ))}
              {bounty.tags.length > tagPreview.length && (
                <span className="text-[11px] text-ink-faint">
                  +{bounty.tags.length - tagPreview.length} more
                </span>
              )}
            </div>
          )}
        </div>

        <div className="grid grid-cols-2 gap-2 text-xs sm:grid-cols-4 lg:grid-cols-2">
          <SummaryMetric label="trials" value={summary.evidence.linkedTrials} size="compact" />
          <SummaryMetric label="claimed" value={bounty.claimed_agent_name || "-"} size="compact" />
          <SummaryMetric label="role" value={summary.role} size="compact" />
        </div>

        <ToolbarLink href={href} variant="primary" className="w-full justify-center lg:w-auto">
          Open request
        </ToolbarLink>
      </div>
    </SurfacePanel>
  );
}

function bountyDetailPath(
  view: BountyViewId,
  slug: string,
  section: BountyLifecycleView = "overview",
) {
  const base = `/bounties/${view}/${encodeURIComponent(slug)}`;
  return section === "overview" ? base : `${base}/${section}`;
}

function summarizeBountyMarket(
  bounties: Bounty[] | null,
  options: {
    me: string;
    myAgents: AgentListing[];
    trialRooms: TrialRoom[];
  },
): BountyMarketSummary {
  const summary: BountyMarketSummary = {
    total: bounties?.length ?? 0,
    open: 0,
    mine: 0,
    claimed: 0,
    fulfilled: 0,
    cancelled: 0,
    claimable: 0,
    readyToFulfill: 0,
    needsEvidence: 0,
    linkedTrials: 0,
    receipts: 0,
    firstOpen: null,
    firstClaimable: null,
    firstNeedsEvidence: null,
    firstReadyToFulfill: null,
  };
  if (!bounties) return summary;

  for (const bounty of bounties) {
    const lifecycle = summarizeBountyLifecycle(bounty, {
      me: options.me,
      myAgents: options.myAgents,
      trialRooms: options.trialRooms,
    });

    if (bounty.status === "open") {
      summary.open += 1;
      summary.firstOpen ||= bounty;
    }
    if (bounty.status === "claimed") summary.claimed += 1;
    if (bounty.status === "fulfilled") summary.fulfilled += 1;
    if (bounty.status === "cancelled") summary.cancelled += 1;
    if (bounty.posted_by_email === options.me) summary.mine += 1;

    if (lifecycle.claimable) {
      summary.claimable += 1;
      summary.firstClaimable ||= bounty;
    }

    const hasEvidence =
      lifecycle.evidence.passedRuns > 0 ||
      lifecycle.evidence.receipts > 0 ||
      (lifecycle.evidence.bestScore !== null && lifecycle.evidence.bestScore >= 70);
    if (lifecycle.canFulfill && hasEvidence) {
      summary.readyToFulfill += 1;
      summary.firstReadyToFulfill ||= bounty;
    }

    if (
      bounty.status === "claimed" &&
      !lifecycle.terminal &&
      (lifecycle.evidence.linkedTrials === 0 || lifecycle.evidence.receipts === 0)
    ) {
      summary.needsEvidence += 1;
      summary.firstNeedsEvidence ||= bounty;
    }

    summary.linkedTrials += lifecycle.evidence.linkedTrials;
    summary.receipts += lifecycle.evidence.receipts;
  }

  return summary;
}

function bountyMarketNextAction({
  bounties,
  summary,
  activeFilter,
  selectedBounty,
  creating,
  me,
}: {
  bounties: Bounty[] | null;
  summary: BountyMarketSummary;
  activeFilter: BountyViewId;
  selectedBounty: Bounty | null;
  creating: boolean;
  me: string;
}): BountyMarketNextAction {
  if (!bounties) {
    return {
      label: "Load demand workflow",
      detail: "Fetching bounties, claimable agents, linked trials, and receipt evidence.",
      href: BOUNTY_VIEWS.find((view) => view.id === activeFilter)?.path || "/bounties",
      action: "Refresh view",
      variant: "secondary",
    };
  }
  if (creating) {
    return {
      label: "Finish the request brief",
      detail: "Clear examples and acceptance criteria help agent authors claim the work with less back-and-forth.",
      href: "/bounties/new",
      action: "Keep drafting",
      variant: "primary",
    };
  }
  if (selectedBounty) {
    const section: BountyLifecycleView =
      selectedBounty.status === "open" || selectedBounty.status === "claimed"
        ? "actions"
        : "evidence";
    return {
      label:
        selectedBounty.status === "claimed"
          ? "Review lifecycle actions"
          : selectedBounty.status === "open"
            ? "Decide how this gets claimed"
            : "Audit the request evidence",
      detail: `${selectedBounty.title} is open in a URL-addressable detail flow for brief, evidence, and actions.`,
      href: bountyDetailPath(activeFilter, selectedBounty.slug, section),
      action: section === "actions" ? "Open actions" : "Open evidence",
      variant: "primary",
    };
  }
  if (summary.firstReadyToFulfill) {
    return {
      label: "Close ready owned work",
      detail: `${summary.readyToFulfill} claimed ${summary.readyToFulfill === 1 ? "request has" : "requests have"} enough evidence to review for fulfillment.`,
      href: bountyActionPath(summary.firstReadyToFulfill, me, "actions"),
      action: "Review fulfillment",
      variant: "primary",
    };
  }
  if (summary.firstNeedsEvidence) {
    return {
      label: "Strengthen claimed evidence",
      detail: `${summary.needsEvidence} claimed ${summary.needsEvidence === 1 ? "request is" : "requests are"} missing linked trials or receipts.`,
      href: bountyActionPath(summary.firstNeedsEvidence, me, "evidence"),
      action: "Open evidence",
      variant: "primary",
    };
  }
  if (summary.firstClaimable) {
    return {
      label: "Claim work with a runnable agent",
      detail: `${summary.claimable} request${summary.claimable === 1 ? "" : "s"} can be claimed by one of your deployed public agents.`,
      href: bountyActionPath(summary.firstClaimable, me, "actions"),
      action: "Claim request",
      variant: "primary",
    };
  }
  if (summary.firstOpen) {
    return {
      label: "Inspect open demand",
      detail: "Open requests are available for review, trial setup, or claim preparation.",
      href: bountyDetailPath("open", summary.firstOpen.slug),
      action: "Open request",
      variant: "secondary",
    };
  }
  if (summary.total === 0) {
    return {
      label: "Create the first request",
      detail: "Post a focused job with examples and acceptance criteria to start the market.",
      href: "/bounties/new",
      action: "Post bounty",
      variant: "primary",
    };
  }
  return {
    label: "Review fulfilled work",
    detail: "No active claim work needs attention. Use the fulfilled archive to inspect closed receipts.",
    href: "/bounties/fulfilled",
    action: "Open archive",
    variant: "secondary",
  };
}

function bountyActionPath(
  bounty: Bounty,
  me: string,
  section: BountyLifecycleView = "overview",
) {
  return bountyDetailPath(bountyPreferredView(bounty, me), bounty.slug, section);
}

function bountyPreferredView(bounty: Bounty, me: string): BountyViewId {
  if (me && bounty.posted_by_email === me) return "mine";
  if (bounty.status === "fulfilled") return "fulfilled";
  if (bounty.status === "claimed") return "claimed";
  return "open";
}

function bountyMarketPostureTone(
  bounties: Bounty[] | null,
  summary: BountyMarketSummary,
): StatusBadgeTone {
  if (!bounties) return "neutral";
  if (summary.total === 0) return "neutral";
  if (summary.readyToFulfill > 0) return "emerald";
  if (summary.needsEvidence > 0 || summary.open > 0) return "amber";
  if (summary.fulfilled > 0) return "emerald";
  return "neutral";
}

function bountyMarketPostureLabel(
  bounties: Bounty[] | null,
  summary: BountyMarketSummary,
) {
  if (!bounties) return "loading";
  if (summary.total === 0) return "empty market";
  if (summary.readyToFulfill > 0) return "ready to close";
  if (summary.needsEvidence > 0) return "needs evidence";
  if (summary.claimable > 0) return "claimable work";
  if (summary.open > 0) return "open demand";
  return "clear";
}

function bountyMarketPostureDetail({
  bounties,
  summary,
  visibleCount,
  activeFilter,
  selectedBounty,
  creating,
  loading,
}: {
  bounties: Bounty[] | null;
  summary: BountyMarketSummary;
  visibleCount: number;
  activeFilter: BountyViewId;
  selectedBounty: Bounty | null;
  creating: boolean;
  loading: boolean;
}) {
  if (!bounties) {
    return loading ? "loading requests and linked evidence" : "waiting for bounty data";
  }
  if (creating) return "drafting a new request";
  if (selectedBounty) return `${selectedBounty.status} request detail`;
  const view = BOUNTY_VIEWS.find((item) => item.id === activeFilter)?.label.toLowerCase() || "selected view";
  if (visibleCount !== summary.total) {
    return `${visibleCount.toLocaleString()} of ${summary.total.toLocaleString()} requests in ${view}`;
  }
  return `${summary.total.toLocaleString()} requests across the demand workflow`;
}
