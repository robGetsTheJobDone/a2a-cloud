import { useCallback, useEffect, useMemo, useState } from "react";
import { useLocation, useNavigate, useParams } from "react-router-dom";
import {
  getJob,
  listActivity,
  listJobEvents,
  type Page,
  type WorkEvent,
  type WorkJob,
} from "../api";
import { decodeRouteSegment } from "../navigation";
import { useActivitySelection } from "../providers/ActivityContext";
import {
  CodeBlock,
  EmptyState,
  FilterBar,
  FormField,
  InlineAlert,
  LoadingState,
  SegmentedButton,
  SegmentedControl,
  SelectableSurfaceButton,
  SelectInput,
  SummaryMetric,
  SurfacePanel,
  TextInput,
  ToolbarButton,
} from "./DashboardChrome";
import { StateBadge, StatusBadge } from "./StatusPillAdapters";
import { DetailSheet } from "./ListDetailLayout";
import {
  DASHBOARD_SECTION_CACHE_KEYS,
  dashboardScopedCacheKey,
  useDashboardSectionResource,
  useDashboardSectionState,
} from "./DashboardSectionCache";
import { RoutePageShell } from "./RoutePageShell";
import {
  DashboardSurfacePosture,
  type SurfacePostureStatus,
} from "./SurfacePosture";
import {
  activityFilterBadges,
  parseActivityUrlFilters,
  type ActivityUrlFilters,
} from "./activityUtils";

const PAGE_SIZE = 50;
const EVENT_PAGE_SIZE = 18;
const ACTIVE_STATUSES = new Set(["queued", "waiting", "pending", "running"]);
const DONE_STATUSES = new Set(["complete", "succeeded", "success", "passed", "live", "done", "ok"]);
const FAILED_STATUSES = new Set(["error", "failed", "failure", "canceled", "cancelled", "denied"]);

const BASE_SOURCE_OPTIONS = [
  "subagent",
  "dag",
  "trial",
  "proof",
  "deployment",
  "llm",
  "manual",
];

const BASE_STATUS_OPTIONS = [
  "queued",
  "waiting",
  "running",
  "complete",
  "succeeded",
  "error",
  "failed",
  "canceled",
  "cancelled",
];

type EventPageLike =
  | Page<WorkEvent>
  | WorkEvent[]
  | {
      items?: WorkEvent[];
      events?: WorkEvent[];
      next_cursor?: string | null;
    };

type ActivityDetailView = "overview" | "payload" | "events";

const ACTIVITY_DETAIL_VIEWS: { id: ActivityDetailView; label: string }[] = [
  { id: "overview", label: "Overview" },
  { id: "payload", label: "Payload" },
  { id: "events", label: "Events" },
];

type ActivityListResource = {
  jobs: WorkJob[];
  cursor: string | null;
};

function activityDetailViewForSection(section: string | null | undefined): ActivityDetailView {
  if (section === "payload") return "payload";
  if (section === "events" || section === "receipt") return "events";
  return "overview";
}

export function Activity() {
  const location = useLocation();
  const navigate = useNavigate();
  const {
    jobId: encodedJobId,
    section: routeSection,
  } = useParams<{ jobId?: string; section?: string }>();
  const routeJobId = decodeRouteSegment(encodedJobId);

  // Mandate B/C/E: run selection lives in the shared ActivityContext, never in
  // the route. Detail opens as an in-place right-side sheet over the kept-alive
  // ledger list — selecting a job no longer navigates and never unmounts the
  // list. Deep-linked /activity/:jobId[/:section] URLs hydrate the selection on
  // first paint (render-time, so SSR + initial mount resolve identically) until
  // the user interacts, after which the context selection is authoritative.
  const { selectedRunId, selectedRunSection, setSelectedRun, setSelectedRunSection } =
    useActivitySelection();
  const [userTouchedSelection, setUserTouchedSelection] = useState(false);
  const selectedJobId =
    userTouchedSelection || selectedRunId ? selectedRunId : routeJobId;
  const detailView = activityDetailViewForSection(
    userTouchedSelection || selectedRunId ? selectedRunSection : routeSection,
  );

  const selectJob = useCallback(
    (jobId: string) => {
      setUserTouchedSelection(true);
      setSelectedRun(jobId, "overview");
    },
    [setSelectedRun],
  );
  const closeDetail = useCallback(() => {
    setUserTouchedSelection(true);
    setSelectedRun(null);
  }, [setSelectedRun]);
  const selectView = useCallback(
    (view: ActivityDetailView) => {
      setUserTouchedSelection(true);
      setSelectedRunSection(view);
    },
    [setSelectedRunSection],
  );

  const urlFilters = useMemo(
    () => parseActivityUrlFilters(location.search),
    [location.search],
  );
  const [sourceFilter, setSourceFilter] = useDashboardSectionState(
    DASHBOARD_SECTION_CACHE_KEYS.operate.activitySourceFilter,
    urlFilters.source ?? "all",
  );
  const [statusFilter, setStatusFilter] = useDashboardSectionState(
    DASHBOARD_SECTION_CACHE_KEYS.operate.activityStatusFilter,
    urlFilters.status ?? "all",
  );
  const [query, setQuery] = useDashboardSectionState(
    DASHBOARD_SECTION_CACHE_KEYS.operate.activityQuery,
    urlFilters.q ?? "",
  );
  const [debouncedQuery, setDebouncedQuery] = useState(() => urlFilters.q ?? query);
  const [loadingMore, setLoadingMore] = useState(false);
  const [actionErr, setActionErr] = useState<string | null>(null);
  const [selectedJob, setSelectedJob] = useState<WorkJob | null>(null);
  const [selectedEvents, setSelectedEvents] = useState<WorkEvent[] | null>(null);
  const [eventCursor, setEventCursor] = useState<string | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailErr, setDetailErr] = useState<string | null>(null);
  const [eventsLoadingMore, setEventsLoadingMore] = useState(false);
  const urlFilterBadges = useMemo(
    () => activityFilterBadges(urlFilters),
    [urlFilters],
  );
  const hasUrlFilters = urlFilterBadges.length > 0;
  const activeSourceFilter = urlFilters.source ?? sourceFilter;
  const activeStatusFilter = urlFilters.status ?? statusFilter;
  const activeQuery = urlFilters.q ?? query;

  const clearUrlFilters = useCallback(() => {
    if (hasUrlFilters) {
      navigate(location.pathname, { replace: true });
    }
  }, [hasUrlFilters, location.pathname, navigate]);

  const updateSourceFilter = useCallback(
    (value: string) => {
      setSourceFilter(value);
      clearUrlFilters();
    },
    [clearUrlFilters, setSourceFilter],
  );

  const updateStatusFilter = useCallback(
    (value: string) => {
      setStatusFilter(value);
      clearUrlFilters();
    },
    [clearUrlFilters, setStatusFilter],
  );

  const updateQuery = useCallback(
    (value: string) => {
      setQuery(value);
      clearUrlFilters();
    },
    [clearUrlFilters, setQuery],
  );

  useEffect(() => {
    const handle = window.setTimeout(() => setDebouncedQuery(activeQuery), 250);
    return () => window.clearTimeout(handle);
  }, [activeQuery]);

  const filters = useMemo(
    () => ({
      agent: urlFilters.agent,
      grant: urlFilters.grant,
      kind: urlFilters.kind,
      source: activeSourceFilter === "all" ? undefined : activeSourceFilter,
      status: activeStatusFilter === "all" ? undefined : activeStatusFilter,
      thread_id: urlFilters.thread_id,
      type: urlFilters.type,
      q: debouncedQuery.trim() || undefined,
    }),
    [activeSourceFilter, activeStatusFilter, debouncedQuery, urlFilters],
  );

  const listCacheKey = useMemo(
    () => dashboardScopedCacheKey(
      DASHBOARD_SECTION_CACHE_KEYS.operate.activityListPrefix,
      JSON.stringify(filters),
    ),
    [filters],
  );

  const loadActivityList = useCallback(async (): Promise<ActivityListResource> => {
    const page = await listActivity({ ...filters, limit: PAGE_SIZE });
    return {
      jobs: page.items,
      cursor: page.next_cursor,
    };
  }, [filters]);

  const {
    data: activityList,
    error: loadErr,
    loading,
    refresh: refreshActivity,
    setData: setActivityList,
  } = useDashboardSectionResource(listCacheKey, loadActivityList);
  const jobs = activityList?.jobs ?? null;
  const cursor = activityList?.cursor ?? null;
  const err = actionErr ?? loadErr;

  const refresh = useCallback(async () => {
    setActionErr(null);
    try {
      await refreshActivity();
    } catch {
      // The section cache stores and exposes the load error.
    }
  }, [refreshActivity]);

  const loadMore = useCallback(async () => {
    if (!cursor || loadingMore) return;
    setLoadingMore(true);
    try {
      const page = await listActivity({
        ...filters,
        cursor,
        limit: PAGE_SIZE,
      });
      setActivityList((current) => ({
        jobs: uniqueJobs([...(current?.jobs || []), ...page.items]),
        cursor: page.next_cursor,
      }));
      setActionErr(null);
    } catch (ex) {
      setActionErr(errorText(ex));
    } finally {
      setLoadingMore(false);
    }
  }, [cursor, filters, loadingMore, setActivityList]);

  useEffect(() => {
    if (!selectedJobId && (!jobs || jobs.length === 0)) {
      setSelectedJob(null);
      setSelectedEvents(null);
      setEventCursor(null);
      setDetailErr(null);
    }
  }, [jobs, selectedJobId]);

  const selectedListJob = useMemo(
    () => jobs?.find((job) => job.job_id === selectedJobId) || null,
    [jobs, selectedJobId],
  );

  useEffect(() => {
    let active = true;
    if (!selectedJobId) {
      setSelectedJob(null);
      setSelectedEvents(null);
      setEventCursor(null);
      setDetailErr(null);
      return () => {
        active = false;
      };
    }

    setDetailLoading(true);
    setDetailErr(null);
    setSelectedJob(selectedListJob);
    setSelectedEvents(null);
    setEventCursor(null);

    Promise.allSettled([
      getJob(selectedJobId),
      listJobEvents(selectedJobId, { limit: EVENT_PAGE_SIZE }),
    ]).then(([jobResult, eventResult]) => {
      if (!active) return;
      const nextJob =
        jobResult.status === "fulfilled"
          ? mergeJobDetail(jobResult.value, selectedListJob)
          : selectedListJob;
      const fallbackEvents = nextJob?.events || [];
      const nextEvents =
        eventResult.status === "fulfilled"
          ? normalizeEventPage(eventResult.value).items
          : fallbackEvents;
      setSelectedJob(nextJob);
      setSelectedEvents(nextEvents.length > 0 ? nextEvents : fallbackEvents);
      setEventCursor(
        eventResult.status === "fulfilled"
          ? normalizeEventPage(eventResult.value).nextCursor
          : null,
      );
      setDetailErr(detailError(jobResult, eventResult));
    }).finally(() => {
      if (active) setDetailLoading(false);
    });

    return () => {
      active = false;
    };
  }, [selectedJobId, selectedListJob]);

  const loadMoreEvents = useCallback(async () => {
    if (!selectedJobId || !eventCursor || eventsLoadingMore) return;
    setEventsLoadingMore(true);
    try {
      const page = normalizeEventPage(
        await listJobEvents(selectedJobId, {
          cursor: eventCursor,
          limit: EVENT_PAGE_SIZE,
        }),
      );
      setSelectedEvents((current) => [...(current || []), ...page.items]);
      setEventCursor(page.nextCursor);
      setDetailErr(null);
    } catch (ex) {
      setDetailErr(`Events: ${errorText(ex)}`);
    } finally {
      setEventsLoadingMore(false);
    }
  }, [eventCursor, eventsLoadingMore, selectedJobId]);

  const sourceOptions = useMemo(
    () => buildOptions(BASE_SOURCE_OPTIONS, jobs?.map(jobSource) || [], activeSourceFilter),
    [activeSourceFilter, jobs],
  );
  const statusOptions = useMemo(
    () => buildOptions(BASE_STATUS_OPTIONS, jobs?.map((job) => job.status) || [], activeStatusFilter),
    [activeStatusFilter, jobs],
  );
  const visibleCount = jobs?.length || 0;
  const summary = useMemo(() => summarizeActivity(jobs || []), [jobs]);
  const visibleFilterState = useMemo<ActivityUrlFilters>(
    () => ({
      ...filters,
      q: activeQuery.trim() || undefined,
    }),
    [activeQuery, filters],
  );
  const activeFilters = useMemo(
    () => activityFilterBadges(visibleFilterState),
    [visibleFilterState],
  );
  const activeFilterCount = activeFilters.length;
  const hasFilters = activeFilterCount > 0;

  function clearFilters() {
    setSourceFilter("all");
    setStatusFilter("all");
    setQuery("");
    clearUrlFilters();
  }

  return (
    <RoutePageShell
      routeId="activity"
      title="Activity"
      description="Search and review recent jobs across agents, trials, proofs, deployments, and LLM work."
      data-onboarding-target="activity-page"
      layout="full-height"
      actions={
        <div className="flex min-w-0 flex-col gap-2 sm:w-[360px]">
          <TextInput
            data-onboarding-target="activity-search"
            value={activeQuery}
            onChange={(event) => updateQuery(event.target.value)}
            placeholder="Search title, kind, id, summary"
          />
          <div className="flex items-center justify-between text-[11px] text-ink-faint">
            <span>{hasFilters ? `${activeFilterCount} filters active` : "Showing latest work"}</span>
            {hasFilters && (
              <ToolbarButton
                onClick={clearFilters}
                variant="ghost"
                size="xs"
              >
                Clear filters
              </ToolbarButton>
            )}
          </div>
        </div>
      }
    >
      <DashboardSurfacePosture
        data-onboarding-target="activity-posture"
        eyebrow="Work ledger"
        title={activityPostureDetail(jobs, summary, loading, hasFilters, activeFilterCount)}
        status={activityPostureStatus(jobs, summary)}
        metrics={[
          { label: "shown", value: visibleCount.toLocaleString() },
          { label: "active", value: summary.active.toLocaleString(), tone: "authority" },
          { label: "done", value: summary.done.toLocaleString(), tone: "live" },
          { label: "failed", value: summary.failed.toLocaleString(), tone: "danger" },
          { label: "sources", value: summary.sources.toLocaleString() },
        ]}
        actions={
          hasFilters ? (
            <ToolbarButton onClick={clearFilters} size="sm">
              Clear filters
            </ToolbarButton>
          ) : null
        }
      />

      {activeFilters.length > 0 && (
        <div className="flex flex-wrap items-center gap-1.5 text-[11px] text-ink-faint">
          <span className="uppercase tracking-wide">Filtered by</span>
          {activeFilters.map((filter) => (
            <span
              key={`${filter.key}:${filter.value}`}
              className="max-w-full truncate rounded-md border border-runtime-line-soft/70 bg-runtime-panel/60 px-2 py-1 text-ink-soft"
              title={`${filter.label}: ${filter.value}`}
            >
              <span className="text-ink-muted">{filter.label}</span>{" "}
              <span className="font-mono">{filter.value}</span>
            </span>
          ))}
        </div>
      )}

      <div className="flex min-h-0 flex-1 overflow-hidden">
        <SurfacePanel
          as="section"
          data-onboarding-target="activity-list"
          className="flex min-h-0 w-full flex-col overflow-hidden bg-runtime-bg/70"
        >
          <div className="border-b border-runtime-line-soft/60 bg-runtime-bg/90 p-3">
            <FilterBar
              actions={
                <div className="flex items-center gap-3">
                  <span className="text-xs text-ink-faint">
                    {loading ? "updating" : `${visibleCount} jobs`}
                  </span>
                  <ToolbarButton onClick={refresh} disabled={loading}>
                    {loading ? "Refreshing..." : "Refresh"}
                  </ToolbarButton>
                </div>
              }
            >
              <FormField label="Source">
                <SelectInput
                  value={activeSourceFilter}
                  onChange={(event) => updateSourceFilter(event.target.value)}
                  className="min-w-36"
                >
                  <option value="all">all sources</option>
                  {sourceOptions.map((source) => (
                    <option key={source} value={source}>
                      {labelFor(source)}
                    </option>
                  ))}
                </SelectInput>
              </FormField>
              <FormField label="Status">
                <SelectInput
                  value={activeStatusFilter}
                  onChange={(event) => updateStatusFilter(event.target.value)}
                  className="min-w-36"
                >
                  <option value="all">all statuses</option>
                  {statusOptions.map((status) => (
                    <option key={status} value={status}>
                      {labelFor(status)}
                    </option>
                  ))}
                </SelectInput>
              </FormField>
              <div className="min-w-0">
                <div className="mb-1 text-xs text-ink-muted">Quick status</div>
                <SegmentedControl
                  aria-label="Quick status filter"
                  className="flex flex-wrap"
                >
                  {["all", "running", "waiting", "complete", "error"].map((status) => (
                    <SegmentedButton
                      key={status}
                      onClick={() => updateStatusFilter(status)}
                      selected={activeStatusFilter === status}
                      className="h-7 text-xs"
                    >
                      {status === "all" ? "All" : labelFor(status)}
                    </SegmentedButton>
                  ))}
                </SegmentedControl>
              </div>
            </FilterBar>
          </div>

          {err && (
            <div className="border-b border-runtime-line-soft/60 px-4 py-3">
              <InlineAlert tone="red">{err}</InlineAlert>
            </div>
          )}

          <div className="min-h-0 flex-1 overflow-auto p-3">
            {jobs === null ? (
              <LoadingState label="Loading activity..." />
            ) : jobs.length === 0 ? (
              <EmptyState
                title="No activity matches this view"
                description="Try a broader search term, source, or status."
              />
            ) : (
              <div className="space-y-2">
                {jobs.length > 0 && (
                  <div className="flex items-center justify-between px-1 pb-1 text-[11px] uppercase text-ink-faint">
                    <span>Recent work</span>
                    <span>Newest first</span>
                  </div>
                )}
                {jobs.map((job) => (
                  <ActivityRow
                    key={job.job_id}
                    job={job}
                    onSelect={() => selectJob(job.job_id)}
                    selected={job.job_id === selectedJobId}
                  />
                ))}
                {cursor && (
                  <ToolbarButton
                    onClick={loadMore}
                    disabled={loadingMore}
                    className="w-full"
                  >
                    {loadingMore ? "Loading..." : "Load more"}
                  </ToolbarButton>
                )}
              </div>
            )}
          </div>
        </SurfacePanel>
      </div>

      <ActivityDetailSheet
        open={Boolean(selectedJobId)}
        onClose={closeDetail}
        jobId={selectedJobId}
        job={selectedJob}
        events={selectedEvents}
        view={detailView}
        onSelectView={selectView}
        loading={detailLoading}
        error={detailErr}
        eventCursor={eventCursor}
        loadingMoreEvents={eventsLoadingMore}
        onLoadMoreEvents={loadMoreEvents}
      />
    </RoutePageShell>
  );
}

function ActivityRow({
  job,
  onSelect,
  selected,
}: {
  job: WorkJob;
  onSelect: () => void;
  selected: boolean;
}) {
  const source = jobSource(job);
  const title = jobTitle(job);
  const message = job.error || job.summary || "No summary yet.";
  const updated = job.updated_at || job.created_at;
  const duration = jobDuration(job);
  const family = statusFamily(job.status);
  return (
    <SelectableSurfaceButton
      onClick={onSelect}
      selected={selected}
      className={family === "failed" ? "border-signal-danger/50 ring-signal-danger/10" : undefined}
    >
      <div className="flex items-start gap-3">
        <span
          aria-hidden="true"
          className={
            "mt-1 h-10 w-1 rounded-full " +
            (family === "done"
              ? "bg-signal-live/70"
              : family === "failed"
                ? "bg-signal-danger/70"
                : family === "active"
                  ? "bg-signal-authority/70"
                  : "bg-runtime-line")
          }
        />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="flex min-w-0 items-center gap-2">
              <StateBadge status={job.status} />
              <StatusBadge tone="neutral">
                {source}
              </StatusBadge>
              <span className="truncate text-sm font-medium text-ink">
                {title}
              </span>
            </div>
            <span className="shrink-0 text-[11px] text-ink-faint">
              {relativeDate(updated)}
            </span>
          </div>
          <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px] text-ink-faint">
            <span className="max-w-full truncate font-mono">{job.job_id}</span>
            <span>{job.kind}</span>
            {duration && <span>{duration}</span>}
            {job.agent_name && <span>{job.agent_name}</span>}
            {job.skill_name && <span>{job.skill_name}</span>}
          </div>
          <p
            className={
              "mt-2 line-clamp-2 text-xs leading-relaxed " +
              (job.error ? "text-signal-danger" : "text-ink-dim")
            }
          >
            {message}
          </p>
        </div>
      </div>
    </SelectableSurfaceButton>
  );
}

function activityPostureStatus(
  jobs: WorkJob[] | null,
  summary: ReturnType<typeof summarizeActivity>,
): SurfacePostureStatus {
  const tone = activityPostureTone(jobs, summary);
  return {
    label: activityPostureLabel(jobs, summary),
    tone: postureStatusTone(tone),
    pulse: tone === "amber",
    dot: tone === "emerald" || tone === "amber",
  };
}

function postureStatusTone(
  tone: ReturnType<typeof activityPostureTone>,
): SurfacePostureStatus["tone"] {
  if (tone === "red") return "danger";
  if (tone === "amber") return "authority";
  if (tone === "emerald") return "live";
  return "neutral";
}

function ActivityMetaStrip({ job }: { job: WorkJob }) {
  const items = [
    { label: "source", value: jobSource(job) },
    { label: "kind", value: job.kind },
    { label: "duration", value: jobDuration(job) || "in progress" },
    { label: "events", value: String(job.events?.length || 0) },
  ];
  return (
    <div className="grid gap-2 sm:grid-cols-4">
      {items.map((item) => (
        <SummaryMetric
          key={item.label}
          label={item.label}
          value={item.value}
          size="compact"
        />
      ))}
    </div>
  );
}

function PreviewBlock({
  label,
  value,
}: {
  label: string;
  value: Record<string, unknown> | undefined;
}) {
  const text = compactPayload(value);
  if (!text) return null;
  return (
    <CodeBlock label={label} className="max-h-40 p-2 text-[11px] text-ink-dim">
      {text}
    </CodeBlock>
  );
}

/**
 * ActivityDetailSheet — mandate C: the selected job opens as an in-place
 * right-side sheet layered over the kept-alive ledger list (the list never
 * unmounts). The header is compacted to a single posture strip (mandate D).
 */
function ActivityDetailSheet({
  open,
  onClose,
  jobId,
  job,
  events,
  view,
  onSelectView,
  loading,
  error,
  eventCursor,
  loadingMoreEvents,
  onLoadMoreEvents,
}: {
  open: boolean;
  onClose: () => void;
  jobId: string | null;
  job: WorkJob | null;
  events: WorkEvent[] | null;
  view: ActivityDetailView;
  onSelectView: (view: ActivityDetailView) => void;
  loading: boolean;
  error: string | null;
  eventCursor: string | null;
  loadingMoreEvents: boolean;
  onLoadMoreEvents: () => void;
}) {
  const headingId = job?.job_id || jobId || "loading";
  const title = (
    <div className="flex min-w-0 items-center gap-2">
      <span className="text-[10px] uppercase tracking-[0.12em] text-ink-faint">Job</span>
      <span className="truncate font-mono text-sm text-ink">{headingId}</span>
      {job && <StateBadge status={job.status} />}
    </div>
  );

  return (
    <DetailSheet
      open={open}
      onClose={onClose}
      size="lg"
      title={title}
      description={job ? jobTitle(job) : undefined}
    >
      <div data-onboarding-target="activity-detail" className="space-y-3">
        {!job && (loading || jobId) ? (
          <LoadingState label="Loading selected job..." />
        ) : !job ? (
          <EmptyState
            title="No job selected"
            description="Select a job from the activity list to inspect its status and events."
          />
        ) : (
          <>
            {error && <InlineAlert tone="red">{error}</InlineAlert>}
            {loading && (
              <SurfacePanel
                as="div"
                className="bg-runtime-panel/40 px-3 py-2 text-xs text-ink-muted"
              >
                Updating selected job...
              </SurfacePanel>
            )}

            <SegmentedControl
              role="tablist"
              aria-label="Activity detail views"
              className="flex flex-wrap gap-1 bg-runtime-bg"
            >
              {ACTIVITY_DETAIL_VIEWS.map((entry) => (
                <SegmentedButton
                  key={entry.id}
                  onClick={() => onSelectView(entry.id)}
                  selected={view === entry.id}
                >
                  {entry.label}
                </SegmentedButton>
              ))}
            </SegmentedControl>

            {view === "payload" ? (
              <ActivityPayloadView job={job} />
            ) : view === "events" ? (
              <ActivityEventsView
                events={events}
                eventCursor={eventCursor}
                loadingMoreEvents={loadingMoreEvents}
                onLoadMoreEvents={onLoadMoreEvents}
              />
            ) : (
              <ActivityJobOverview job={job} />
            )}
          </>
        )}
      </div>
    </DetailSheet>
  );
}

function ActivityJobOverview({ job }: { job: WorkJob }) {
  return (
    <>
      <ActivityMetaStrip job={job} />

      <div>
        <div className="flex flex-wrap items-center gap-2">
          <h2 className="min-w-0 truncate text-base font-semibold text-ink">
            {jobTitle(job)}
          </h2>
          <StatusBadge tone="neutral">
            {job.kind}
          </StatusBadge>
        </div>
        {job.error ? (
          <InlineAlert tone="red" className="mt-2 [overflow-wrap:anywhere]">
            {job.error}
          </InlineAlert>
        ) : job.summary ? (
          <SurfacePanel
            as="div"
            className="mt-2 whitespace-pre-wrap bg-runtime-panel/40 px-3 py-2 text-sm leading-relaxed text-ink-soft"
          >
            {job.summary}
          </SurfacePanel>
        ) : null}
      </div>

      <div className="grid gap-2 text-xs sm:grid-cols-2">
        <SummaryMetric label="created" value={formatDate(job.created_at)} size="compact" />
        <SummaryMetric label="updated" value={formatDate(job.updated_at)} size="compact" />
        <SummaryMetric label="started" value={formatDate(job.started_at)} size="compact" />
        <SummaryMetric label="completed" value={formatDate(job.completed_at)} size="compact" />
      </div>

      <div className="flex flex-wrap gap-1.5">
        {jobChips(job).map((chip) => (
          <span
            key={`${chip.label}:${chip.value}`}
            className="rounded-md border border-runtime-line-soft/60 bg-runtime-panel/60 px-1.5 py-0.5 text-[10px] text-ink-muted"
          >
            {chip.label}:{" "}
            <span className="font-mono text-ink-dim">{chip.value}</span>
          </span>
        ))}
      </div>
    </>
  );
}

function ActivityPayloadView({
  job,
}: {
  job: WorkJob;
}) {
  const fileOpsText =
    job.file_ops && job.file_ops.length > 0
      ? JSON.stringify(job.file_ops, null, 2)
      : null;
  const hasPayload = Boolean(
    compactPayload(job.args_preview) ||
    compactPayload(job.result_preview) ||
    compactPayload(job.metadata) ||
    fileOpsText,
  );
  return (
    <SurfacePanel as="section" className="space-y-3 bg-runtime-bg">
      <div className="border-b border-runtime-line-soft/60 px-3 py-2">
        <div className="text-xs font-medium text-ink-soft">
          Payload
        </div>
        <div className="text-[11px] text-ink-faint">
          Args, result preview, metadata, and file operations for this job.
        </div>
      </div>
      <div className="grid gap-3 p-3">
        {hasPayload ? (
          <>
            <PreviewBlock label="Args preview" value={job.args_preview} />
            <PreviewBlock label="Result preview" value={job.result_preview} />
            <PreviewBlock label="Metadata" value={job.metadata} />
            {fileOpsText && (
              <CodeBlock label="File operations" className="max-h-40 p-2 text-[11px] text-ink-dim">
                {fileOpsText}
              </CodeBlock>
            )}
          </>
        ) : (
          <EmptyState title="No payload fields" size="compact" />
        )}
      </div>
    </SurfacePanel>
  );
}

function ActivityEventsView({
  events,
  eventCursor,
  loadingMoreEvents,
  onLoadMoreEvents,
}: {
  events: WorkEvent[] | null;
  eventCursor: string | null;
  loadingMoreEvents: boolean;
  onLoadMoreEvents: () => void;
}) {
  return (
    <ActivityEventsPanel
      events={events}
      eventCursor={eventCursor}
      loadingMoreEvents={loadingMoreEvents}
      onLoadMoreEvents={onLoadMoreEvents}
    />
  );
}

function ActivityEventsPanel({
  events,
  eventCursor,
  loadingMoreEvents,
  onLoadMoreEvents,
}: {
  events: WorkEvent[] | null;
  eventCursor: string | null;
  loadingMoreEvents: boolean;
  onLoadMoreEvents: () => void;
}) {
  return (
    <SurfacePanel as="section" className="overflow-hidden bg-runtime-bg">
      <div className="flex items-center justify-between gap-3 border-b border-runtime-line-soft/60 px-3 py-2">
        <div>
          <div className="text-xs font-medium text-ink-soft">
            Job events
          </div>
          <div className="text-[11px] text-ink-faint">
            {events ? `${events.length} shown` : "loading"}
          </div>
        </div>
        {eventCursor && (
          <ToolbarButton
            onClick={onLoadMoreEvents}
            disabled={loadingMoreEvents}
            size="xs"
          >
            {loadingMoreEvents ? "Loading..." : "More"}
          </ToolbarButton>
        )}
      </div>
      {events === null ? (
        <div className="p-3 text-sm text-ink-muted">Loading events...</div>
      ) : events.length === 0 ? (
        <EmptyState title="No events recorded" size="compact" />
      ) : (
        <div className="relative divide-y divide-runtime-line-soft before:absolute before:bottom-3 before:left-[18px] before:top-3 before:w-px before:bg-runtime-line-soft/70">
          {events.map((event) => (
            <EventRow key={eventKey(event)} event={event} />
          ))}
        </div>
      )}
    </SurfacePanel>
  );
}

function EventRow({ event }: { event: WorkEvent }) {
  const name = event.event_type || event.type || "event";
  const text = eventMessage(event);
  const payload = compactPayload(event.payload);
  const family = statusFamily(event.status || event.severity || "");
  return (
    <div className="relative px-3 py-2 pl-9 text-xs">
      <span
        aria-hidden="true"
        className={
          "absolute left-3 top-4 h-2.5 w-2.5 rounded-full ring-4 ring-runtime-bg " +
          (family === "done"
            ? "bg-signal-live"
            : family === "failed"
              ? "bg-signal-danger"
              : family === "active"
                ? "bg-signal-authority"
                : "bg-ink-faint")
        }
      />
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex min-w-0 items-center gap-2">
          <span className="truncate font-mono text-ink-soft">{name}</span>
          {event.status && <StateBadge status={event.status} size="xs" />}
          {event.stage && (
            <StatusBadge tone="neutral" className="px-1.5 text-[10px] text-ink-muted">
              {event.stage}
            </StatusBadge>
          )}
          {event.severity && (
            <StatusBadge tone="neutral" className="px-1.5 text-[10px] text-ink-muted">
              {event.severity}
            </StatusBadge>
          )}
        </div>
        <span className="text-[11px] text-ink-faint">
          {relativeDate(event.created_at)}
        </span>
      </div>
      {text && <div className="mt-1 text-ink-dim">{text}</div>}
      {payload && (
        <CodeBlock className="mt-2 max-h-24 bg-runtime-panel/40 p-2 text-[11px] text-ink-muted">
          {payload}
        </CodeBlock>
      )}
    </div>
  );
}

function summarizeActivity(jobs: WorkJob[]): {
  active: number;
  done: number;
  failed: number;
  sources: number;
} {
  const sources = new Set<string>();
  let active = 0;
  let done = 0;
  let failed = 0;
  for (const job of jobs) {
    sources.add(jobSource(job));
    const family = statusFamily(job.status);
    if (family === "active") active += 1;
    if (family === "done") done += 1;
    if (family === "failed") failed += 1;
  }
  return { active, done, failed, sources: sources.size };
}

function activityPostureTone(
  jobs: WorkJob[] | null,
  summary: ReturnType<typeof summarizeActivity>,
) {
  if (!jobs) return "neutral" as const;
  if (summary.failed > 0) return "red" as const;
  if (summary.active > 0) return "amber" as const;
  if (summary.done > 0) return "emerald" as const;
  return "neutral" as const;
}

function activityPostureLabel(
  jobs: WorkJob[] | null,
  summary: ReturnType<typeof summarizeActivity>,
) {
  if (!jobs) return "checking";
  if (summary.failed > 0) return "needs review";
  if (summary.active > 0) return "active";
  if (summary.done > 0) return "stable";
  return "empty";
}

function activityPostureDetail(
  jobs: WorkJob[] | null,
  summary: ReturnType<typeof summarizeActivity>,
  loading: boolean,
  hasFilters: boolean,
  activeFilterCount: number,
) {
  if (!jobs) return "loading latest work";
  if (loading) return "refreshing activity";
  const filterText = hasFilters ? ` with ${activeFilterCount} filter${activeFilterCount === 1 ? "" : "s"}` : "";
  if (summary.failed > 0) {
    return `${summary.failed} failed job${summary.failed === 1 ? "" : "s"} shown${filterText}`;
  }
  if (summary.active > 0) {
    return `${summary.active} active job${summary.active === 1 ? "" : "s"} shown${filterText}`;
  }
  if (jobs.length > 0) return `${jobs.length} recent job${jobs.length === 1 ? "" : "s"} shown${filterText}`;
  return hasFilters ? "no jobs match the active filters" : "no recent work loaded";
}

function statusFamily(status: string): "active" | "done" | "failed" | "other" {
  const normalized = status.toLowerCase();
  if (DONE_STATUSES.has(normalized)) return "done";
  if (FAILED_STATUSES.has(normalized)) return "failed";
  if (ACTIVE_STATUSES.has(normalized)) return "active";
  return "other";
}

function normalizeEventPage(page: EventPageLike): {
  items: WorkEvent[];
  nextCursor: string | null;
} {
  if (Array.isArray(page)) return { items: page, nextCursor: null };
  const items =
    "events" in page && Array.isArray(page.events)
      ? page.events
      : "items" in page && Array.isArray(page.items)
        ? page.items
        : [];
  return {
    items,
    nextCursor: page.next_cursor ?? null,
  };
}

function detailError(
  jobResult: PromiseSettledResult<WorkJob>,
  eventResult: PromiseSettledResult<Page<WorkEvent>>,
): string | null {
  const messages: string[] = [];
  if (jobResult.status === "rejected") {
    messages.push(`Job: ${errorText(jobResult.reason)}`);
  }
  if (eventResult.status === "rejected") {
    messages.push(`Events: ${errorText(eventResult.reason)}`);
  }
  return messages.length > 0 ? messages.join(" ") : null;
}

function mergeJobDetail(detail: WorkJob, fallback: WorkJob | null): WorkJob {
  if (!fallback) return detail;
  return {
    ...fallback,
    ...detail,
    source: stringValue(detail["source"]) || stringValue(fallback["source"]) || detail.kind,
  };
}

function uniqueJobs(items: WorkJob[]): WorkJob[] {
  const seen = new Set<string>();
  return items.filter((job) => {
    if (seen.has(job.job_id)) return false;
    seen.add(job.job_id);
    return true;
  });
}

function buildOptions(base: string[], dynamic: string[], selected: string): string[] {
  const values = new Set<string>(base);
  for (const item of dynamic) {
    if (item) values.add(item);
  }
  if (selected !== "all") values.add(selected);
  return Array.from(values);
}

function jobSource(job: WorkJob): string {
  return stringValue(job["source"]) || job.kind || stringValue(job.type) || "work";
}

function jobTitle(job: WorkJob): string {
  const title = job.title?.trim();
  if (title) return title;
  const kind = job.kind || jobSource(job);
  return `${labelFor(kind)} job`;
}

function jobChips(job: WorkJob): { label: string; value: string }[] {
  const candidates: [string, unknown][] = [
    ["source", jobSource(job)],
    ["kind", job.kind],
    ["type", job.type],
    ["agent", job.agent_name],
    ["skill", job.skill_name],
    ["thread", job.thread_id],
    ["grant", job.grant_id],
    ["root", job.root_job_id],
    ["parent", job.parent_job_id],
    ["correlation", job.correlation_id],
  ];
  const chips: { label: string; value: string }[] = [];
  for (const [label, raw] of candidates) {
    const value = chipValue(raw);
    if (value && !chips.some((chip) => chip.label === label && chip.value === value)) {
      chips.push({ label, value });
    }
    if (chips.length >= 8) break;
  }
  return chips;
}

function eventKey(event: WorkEvent): string {
  return event.event_id || `${event.job_id}:${event.id}`;
}

function eventMessage(event: WorkEvent): string | null {
  return (
    stringValue(event.message) ||
    stringValue(event.payload?.["message"]) ||
    stringValue(event.payload?.["summary"]) ||
    stringValue(event.payload?.["error"]) ||
    stringValue(event["summary"])
  );
}

function compactPayload(payload: Record<string, unknown> | null | undefined): string | null {
  if (!payload || Object.keys(payload).length === 0) return null;
  const text = JSON.stringify(payload, null, 2);
  return text.length > 420 ? `${text.slice(0, 420)}...` : text;
}

function chipValue(value: unknown): string | null {
  const text = stringValue(value);
  if (text) return text.length > 42 ? `${text.slice(0, 42)}...` : text;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return null;
}

function stringValue(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

function labelFor(value: string): string {
  return value.replace(/[-_]/g, " ");
}

function jobDuration(job: WorkJob): string | null {
  const start = parseTime(job.started_at || job.created_at);
  const end = parseTime(job.completed_at || job.updated_at);
  if (start === null || end === null || end < start) return null;
  const seconds = Math.max(1, Math.round((end - start) / 1000));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.round(minutes / 60);
  if (hours < 48) return `${hours}h`;
  return `${Math.round(hours / 24)}d`;
}

function relativeDate(value: string | null | undefined): string {
  const time = parseTime(value);
  if (time === null) return "-";
  const seconds = Math.round((Date.now() - time) / 1000);
  const abs = Math.abs(seconds);
  const suffix = seconds >= 0 ? "ago" : "from now";
  if (abs < 60) return `${Math.max(1, abs)}s ${suffix}`;
  const minutes = Math.round(abs / 60);
  if (minutes < 60) return `${minutes}m ${suffix}`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h ${suffix}`;
  const days = Math.round(hours / 24);
  if (days < 14) return `${days}d ${suffix}`;
  return formatDate(value);
}

function parseTime(value: string | null | undefined): number | null {
  if (!value) return null;
  const time = new Date(value).getTime();
  return Number.isNaN(time) ? null : time;
}

function formatDate(value: string | null | undefined): string {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
}

function errorText(ex: unknown): string {
  return ex instanceof Error ? ex.message : String(ex);
}
