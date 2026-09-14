import { useMemo } from "react";
import { useLocation } from "react-router-dom";
import { ControlRoom } from "../../components/ControlRoom";
import {
  FullHeightRouteFrame,
  PersistentRoutePanel,
  ToolbarLink,
} from "../../components/DashboardChrome";
import { DashboardSurfacePosture } from "../../components/SurfacePosture";
import {
  DASHBOARD_SECTION_CACHE_KEYS,
  useDashboardSectionResource,
  useDashboardSectionState,
} from "../../components/DashboardSectionCache";
import {
  budgetPercent,
  controlPostureAction,
  controlPostureDetail,
  controlPostureLabel,
  controlPostureTone,
  isLiveDagRun,
  postureStatusPillTone,
  sortTimeline,
  summarizeTimeline,
} from "../../components/control-room/controlData";
import { getControlRoom } from "../../api";
import type { ControlPolicy } from "../../api";
import { RUNTIME_VIEWS, runtimeViewForPath } from "../../navigation";

const RUNTIME_INITIAL_TIMELINE_LIMIT = 40;

/**
 * RuntimePage — keep-alive switcher (mandate E) whose only chrome is ONE
 * compact DashboardSurfacePosture strip (mandate D). The strip carries the
 * always-visible view nav (mandate A) plus a single posture pill + telemetry
 * derived from the *cached* control-room resources, so
 * switching views never refetches (mandate B). The per-view banner headers and
 * the 523-LOC CommandPosture monolith were folded into this one strip.
 */
export function RuntimePage() {
  const { pathname } = useLocation();
  const view = runtimeViewForPath(pathname);

  return (
    <FullHeightRouteFrame>
      <div className="flex h-full min-h-0 flex-col gap-2 p-2 sm:p-2.5">
        <RuntimeControlPostureBar view={view.label} />

        <div className="min-h-0 flex-1 overflow-hidden">
          <PersistentRoutePanel active className="h-full">
            <ControlRoom activeView={view.id} timelineLimit={RUNTIME_INITIAL_TIMELINE_LIMIT} />
          </PersistentRoutePanel>
        </div>
      </div>
    </FullHeightRouteFrame>
  );
}

function RuntimeViewNav({ activeId }: { activeId: string }) {
  return (
    <nav aria-label="Runtime views" className="flex flex-wrap items-center gap-1">
      {RUNTIME_VIEWS.map((item) => (
        <ToolbarLink
          key={item.id}
          href={item.path}
          size="sm"
          active={item.id === activeId}
          variant={item.id === activeId ? "primary" : "ghost"}
          title={item.description}
        >
          {item.label}
        </ToolbarLink>
      ))}
    </nav>
  );
}

function RuntimeControlPostureBar({ view }: { view: string }) {
  const { data } = useDashboardSectionResource(
    DASHBOARD_SECTION_CACHE_KEYS.runtime.controlRoom,
    () => getControlRoom({ limit: RUNTIME_INITIAL_TIMELINE_LIMIT }),
  );
  const [policy] = useDashboardSectionState<ControlPolicy | null>(
    DASHBOARD_SECTION_CACHE_KEYS.runtime.controlRoomPolicyDraft,
    null,
  );

  const timeline = data?.timeline ?? [];
  const summary = data?.summary ?? null;
  const liveDagRuns = useMemo(
    () => sortTimeline(timeline.filter(isLiveDagRun)),
    [timeline],
  );
  const activity = useMemo(() => summarizeTimeline(timeline), [timeline]);
  const policyDirty = Boolean(
    policy && data?.policy && JSON.stringify(policy) !== JSON.stringify(data.policy),
  );

  if (!summary) {
    return (
      <DashboardSurfacePosture
        data-onboarding-target="runtime-command-posture"
        eyebrow="Admin · Runtime"
        title={view}
        status={{ label: "loading", tone: "neutral", dot: false }}
        actions={<RuntimeViewNav activeId={runtimeIdFromLabel(view)} />}
      />
    );
  }

  const budgetPct = budgetPercent(summary);
  const tone = controlPostureTone({ summary, liveDagRuns, policy, dirty: policyDirty, budgetPct });
  const label = controlPostureLabel({ summary, liveDagRuns, policy, dirty: policyDirty, budgetPct });
  const detail = controlPostureDetail({
    view: "state",
    summary,
    liveDagRuns,
    activity,
    filteredCount: timeline.length,
    sourceName: "All sources",
    policy,
    dirty: policyDirty,
    budgetPct,
  });
  const action = controlPostureAction({
    view: "state",
    summary,
    liveDagRuns,
    activity,
    policy,
    dirty: policyDirty,
    busy: false,
  });
  const pillTone = postureStatusPillTone(tone);

  return (
    <DashboardSurfacePosture
      data-onboarding-target="runtime-command-posture"
      eyebrow="Admin · Runtime"
      title={view}
      status={{
        label,
        tone: pillTone,
        dot: pillTone === "live" || pillTone === "authority",
        pulse: pillTone === "authority",
      }}
      metrics={[
        {
          label: "live DAGs",
          value: liveDagRuns.length.toLocaleString(),
          tone: liveDagRuns.length > 0 ? "authority" : "neutral",
        },
        {
          label: "budget",
          value: summary.monthly_budget_cents > 0 ? `${budgetPct}%` : "—",
          tone: budgetPct >= 90 ? "danger" : budgetPct >= 70 ? "authority" : "neutral",
        },
        {
          label: "failures",
          value: summary.failures.toLocaleString(),
          tone: summary.failures > 0 ? "danger" : "live",
        },
        {
          label: "files",
          value: summary.files_touched.toLocaleString(),
          tone: summary.files_touched > 0 ? "authority" : "neutral",
        },
        { label: "receipts", value: summary.agent_runs.toLocaleString() },
      ]}
      actions={
        <>
          <span className="hidden max-w-xs truncate text-xs text-ink-faint xl:inline">
            {detail}
          </span>
          {action.kind === "link" && (
            <ToolbarLink href={action.href} variant={action.variant} size="sm">
              {action.action}
            </ToolbarLink>
          )}
          <RuntimeViewNav activeId={runtimeIdFromLabel(view)} />
        </>
      }
    />
  );
}

function runtimeIdFromLabel(label: string): string {
  return RUNTIME_VIEWS.find((view) => view.label === label)?.id ?? "state";
}
