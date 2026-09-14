import { useEffect, useMemo, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import {
  getControlReceipt,
  getControlRoom,
  updateControlPolicy,
  type ControlPolicy,
} from "../api";
import {
  EmptyState,
  InlineAlert,
  LiveRegion,
  LoadingState,
  ToolbarButton,
} from "./DashboardChrome";
import {
  DASHBOARD_SECTION_CACHE_KEYS,
  dashboardScopedCacheKey,
  useDashboardSectionResource,
  useDashboardSectionState,
} from "./DashboardSectionCache";
import type { ControlTimelineItem } from "../api";
import {
  buildControlFailureReviewQueue,
  controlReceiptRoute,
  controlTimelineScopeName,
  decodeControlReceiptToken,
  filterControlTimeline,
  isLiveDagRun,
  normalizeControlPolicySection,
  normalizeControlReceiptSection,
  normalizeControlStatusFilter,
  sortTimeline,
  sourceLabel,
  summarizeTimeline,
  type ControlStatusFilter,
  type RuntimeControlViewId,
} from "./control-room/controlData";
import { ControlOverviewView } from "./control-room/Overview";
import { ControlTimelineView } from "./control-room/ControlTimeline";
import { ControlPolicyView } from "./control-room/ControlPolicy";
import { ControlReceiptDetailView } from "./control-room/DetailSheet";

// Re-export the unit-tested pure helpers so existing importers/tests stay green.
export { filterControlTimeline, buildControlFailureReviewQueue };

export function ControlRoom({
  activeView = "state",
  timelineLimit = 40,
}: {
  activeView?: RuntimeControlViewId;
  timelineLimit?: number;
}) {
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const { policySection, receiptToken, receiptSection } = useParams<{
    policySection?: string;
    receiptToken?: string;
    receiptSection?: string;
  }>();
  const routeReceiptPath = decodeControlReceiptToken(receiptToken);
  const activePolicySection = normalizeControlPolicySection(policySection);
  const activeReceiptSection = normalizeControlReceiptSection(receiptSection);
  const {
    data,
    error: loadErr,
    loading,
    refreshing,
    refresh: refreshControlRoom,
    setData: setControlRoomData,
  } = useDashboardSectionResource(
    DASHBOARD_SECTION_CACHE_KEYS.runtime.controlRoom,
    () => getControlRoom({ limit: timelineLimit }),
  );
  const [policy, setPolicy] = useDashboardSectionState<ControlPolicy | null>(
    DASHBOARD_SECTION_CACHE_KEYS.runtime.controlRoomPolicyDraft,
    null,
  );
  const [policyBusy, setPolicyBusy] = useState(false);
  const [policyErr, setPolicyErr] = useState<string | null>(null);
  const [source, setSource] = useDashboardSectionState(
    DASHBOARD_SECTION_CACHE_KEYS.runtime.controlRoomSource,
    "all",
  );
  const status = normalizeControlStatusFilter(searchParams.get("status"));
  const err = loadErr;
  const controlRoomBusy = loading || refreshing || (!data && !err);

  useEffect(() => {
    if (data?.policy && !policy) {
      setPolicy(data.policy);
    }
  }, [data?.policy, policy, setPolicy]);

  const filtered = useMemo(() => {
    return filterControlTimeline(data?.timeline || [], source, status);
  }, [data?.timeline, source, status]);
  const liveDagRuns = useMemo(
    () => sortTimeline((data?.timeline || []).filter(isLiveDagRun)),
    [data?.timeline],
  );
  const recentDagRuns = useMemo(
    () => sortTimeline((data?.timeline || []).filter((row) => row.source === "dag")).slice(0, 4),
    [data?.timeline],
  );
  const activity = useMemo(() => summarizeTimeline(filtered), [filtered]);
  const allActivity = useMemo(() => summarizeTimeline(data?.timeline || []), [data?.timeline]);
  const routeReceiptItem = useMemo(
    () =>
      routeReceiptPath
        ? (data?.timeline || []).find((item) => item.receipt_path === routeReceiptPath) || null
        : null,
    [data?.timeline, routeReceiptPath],
  );
  const sourceName = sourceLabel(source);
  const timelineScopeName = controlTimelineScopeName(sourceName, status);
  const policyDirty = Boolean(
    policy &&
      data?.policy &&
      JSON.stringify(policy) !== JSON.stringify(data.policy),
  );

  const routeReceiptCacheKey = useMemo(
    () => dashboardScopedCacheKey(
      DASHBOARD_SECTION_CACHE_KEYS.runtime.controlReceiptPrefix,
      routeReceiptPath,
    ),
    [routeReceiptPath],
  );
  const {
    data: routeReceipt,
    error: routeReceiptErr,
    loading: routeReceiptLoading,
    refreshing: routeReceiptRefreshing,
  } = useDashboardSectionResource(
    routeReceiptCacheKey,
    () => getControlReceipt(routeReceiptPath || ""),
    { enabled: Boolean(routeReceiptPath) },
  );
  const routeReceiptBusy = routeReceiptLoading || routeReceiptRefreshing;

  async function savePolicy() {
    if (!policy || policyBusy) return;
    setPolicyBusy(true);
    setPolicyErr(null);
    try {
      const next = await updateControlPolicy(policy);
      setPolicy(next);
      setControlRoomData((current) => current ? { ...current, policy: next } : current);
    } catch (ex) {
      setPolicyErr(ex instanceof Error ? ex.message : String(ex));
    } finally {
      setPolicyBusy(false);
    }
  }

  function openReceipt(item: ControlTimelineItem) {
    navigate(controlReceiptRoute(item.receipt_path));
  }

  function closeReceipt() {
    navigate("/runtime/timeline");
  }

  function updateStatusFilter(nextStatus: ControlStatusFilter) {
    const next = new URLSearchParams(searchParams);
    if (nextStatus === "all") {
      next.delete("status");
    } else {
      next.set("status", nextStatus);
    }
    setSearchParams(next, { replace: true });
  }

  return (
    <>
      <LiveRegion>
        {data
          ? [
              `${liveDagRuns.length} live DAG runs.`,
              `${filtered.length} ${timelineScopeName.toLowerCase()} control events shown.`,
              policyDirty ? "Control policy has unsaved changes." : "Control policy saved.",
              routeReceiptBusy ? "Loading control receipt." : "",
              routeReceipt ? `Viewing receipt ${routeReceipt.subject}.` : "",
            ].filter(Boolean).join(" ")
          : err
            ? "Control Room data is unavailable."
            : "Loading Control Room data."}
      </LiveRegion>

      <div
        className="h-full overflow-auto bg-runtime-bg px-4 py-5 sm:px-6 lg:py-6"
        data-onboarding-target={
          routeReceiptPath
            ? "control-receipt"
            : activeView === "policy"
              ? "control-policy"
              : "control-timeline"
        }
        aria-busy={controlRoomBusy || routeReceiptBusy}
      >
        <div className="mx-auto w-full max-w-7xl">
          {err && (
            <div className="mb-4" role="alert">
              <InlineAlert tone="red">{err}</InlineAlert>
            </div>
          )}

          {!data && controlRoomBusy ? (
            <div role="status" aria-live="polite">
              <LoadingState label="Loading controls..." />
            </div>
          ) : !data ? (
            <EmptyState
              title="Runtime controls unavailable"
              description="The control-room ledger did not load. Retry to request a fresh runtime snapshot."
              action={
                <ToolbarButton
                  variant="secondary"
                  onClick={() => {
                    void refreshControlRoom().catch(() => undefined);
                  }}
                >
                  Retry
                </ToolbarButton>
              }
            />
          ) : activeView === "policy" ? (
            <ControlPolicyView
              activeSection={activePolicySection}
              summary={data.summary}
              liveDagRuns={liveDagRuns}
              policy={policy}
              dirty={policyDirty}
              busy={policyBusy}
              error={policyErr}
              onSave={savePolicy}
              onPolicyChange={(next) => setPolicy(next)}
            />
          ) : activeView === "timeline" ? (
            <ControlTimelineView
              liveDagRuns={liveDagRuns}
              recentDagRuns={recentDagRuns}
              filtered={filtered}
              activity={activity}
              source={source}
              status={status}
              scopeName={timelineScopeName}
              onSourceChange={setSource}
              onStatusChange={updateStatusFilter}
              onReceipt={openReceipt}
            />
          ) : (
            <ControlOverviewView
              summary={data.summary}
              liveDagRuns={liveDagRuns}
              recentDagRuns={recentDagRuns}
              timeline={data.timeline}
              activity={allActivity}
              policy={policy}
              dirty={policyDirty}
              onReceipt={openReceipt}
            />
          )}
        </div>
      </div>

      {data && routeReceiptPath && (
        <ControlReceiptDetailView
          receiptPath={routeReceiptPath}
          activeSection={activeReceiptSection}
          trigger={routeReceiptItem}
          receipt={routeReceipt}
          busy={routeReceiptBusy}
          error={routeReceiptErr}
          onClose={closeReceipt}
        />
      )}
    </>
  );
}
