import { useCallback, useEffect, useMemo, useState, type Dispatch, type SetStateAction } from "react";
import { useLocation, useNavigate, useParams } from "react-router-dom";
import {
  createSchedule,
  deleteSchedule,
  listAgents,
  listSchedules,
  runScheduleNow,
  updateSchedule,
  type AgentListing,
  type AgentSchedule,
  type AgentScheduleInput,
  type AgentScheduleTarget,
  type JsonSchema,
} from "../api";
import {
  CodeBlock,
  EmptyState,
  FormField,
  InlineAlert,
  LoadingState,
  PersistentRoutePanel,
  SegmentedButton,
  SegmentedControl,
  SelectableSurfaceLink,
  SelectInput,
  SummaryMetric,
  SurfacePanel,
  TextArea,
  TextInput,
  ToggleField,
  ToolbarButton,
  ToolbarLink,
} from "./DashboardChrome";
import { StateBadge, StatusBadge } from "./StatusPillAdapters";
import {
  DASHBOARD_SECTION_CACHE_KEYS,
  useDashboardSectionResource,
  useDashboardSectionState,
} from "./DashboardSectionCache";
import { scheduleViewForPath } from "../navigation";
import { RoutePageShell } from "./RoutePageShell";
import { DashboardSurfacePosture } from "./SurfacePosture";
import { DetailSheet } from "./ListDetailLayout";
import {
  ChatStructuredInputPanel,
  createStructuredInputValue,
} from "./ChatStructuredInputPanel";

type Draft = {
  name: string;
  enabled: boolean;
  cron: string;
  timezone: string;
  target_type: AgentScheduleTarget;
  prompt: string;
  agent_name: string;
  skill_name: string;
  args_json: string;
};

const DEFAULT_CRON = "0 9 * * *";

const SCHEDULE_DETAIL_SECTIONS = [
  { id: "overview", label: "Overview" },
  { id: "payload", label: "Payload" },
  { id: "metadata", label: "Metadata" },
  { id: "execution", label: "Last execution" },
] as const;

type ScheduleDetailSectionId = (typeof SCHEDULE_DETAIL_SECTIONS)[number]["id"];

type ScheduleResource = {
  schedules: AgentSchedule[];
  agents: AgentListing[];
};

function defaultTimezone() {
  return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
}

function emptyDraft(): Draft {
  return {
    name: "Daily agent run",
    enabled: true,
    cron: DEFAULT_CRON,
    timezone: defaultTimezone(),
    target_type: "main_agent",
    prompt: "",
    agent_name: "",
    skill_name: "",
    args_json: "{}",
  };
}

function scheduleDetailRoute(scheduleId: string) {
  return `/schedules/list/${encodeURIComponent(scheduleId)}`;
}

// A skill's declared input contract drives the structured args form so users
// never hand-edit JSON. Missing/empty schemas fall back to an empty object.
function skillInputSchema(
  agent: AgentListing | null | undefined,
  skillName: string,
): JsonSchema {
  const skill = agent?.card?.skills?.find((item) => item.name === skillName);
  return (skill?.input_schema as JsonSchema | undefined) ?? {};
}

function skillFieldCount(schema: JsonSchema): number {
  return schema.properties ? Object.keys(schema.properties).length : 0;
}

// Seed args_json with the schema's defaults so the form starts pre-filled and
// switching agent/skill resets stale arguments.
function defaultArgsJson(schema: JsonSchema): string {
  if (skillFieldCount(schema) === 0) return "{}";
  return JSON.stringify(createStructuredInputValue(schema), null, 2);
}

function parseArgs(argsJson: string): Record<string, unknown> {
  try {
    const parsed: unknown = JSON.parse(argsJson || "{}");
    return parsed && typeof parsed === "object" && !Array.isArray(parsed)
      ? (parsed as Record<string, unknown>)
      : {};
  } catch {
    return {};
  }
}

function normalizeScheduleDetailSection(
  section: string | null | undefined,
): ScheduleDetailSectionId {
  return SCHEDULE_DETAIL_SECTIONS.some((item) => item.id === section)
    ? (section as ScheduleDetailSectionId)
    : "overview";
}

export function Schedules() {
  const { pathname } = useLocation();
  const navigate = useNavigate();
  const {
    scheduleId: routeScheduleId,
    section: routeScheduleSection,
  } = useParams<{ scheduleId?: string; section?: string }>();
  const view = scheduleViewForPath(pathname);
  const selectedDetailSection = normalizeScheduleDetailSection(routeScheduleSection);
  const loadScheduleResource = useCallback(async (): Promise<ScheduleResource> => {
    const [nextSchedules, nextAgents] = await Promise.all([
      listSchedules(),
      listAgents(),
    ]);
    return {
      schedules: nextSchedules,
      agents: nextAgents,
    };
  }, []);
  const {
    data: scheduleData,
    error: loadErr,
    refresh: refreshSchedules,
    setData: setScheduleData,
  } = useDashboardSectionResource(
    DASHBOARD_SECTION_CACHE_KEYS.operate.schedules,
    loadScheduleResource,
  );
  const schedules = scheduleData?.schedules ?? null;
  const agents = scheduleData?.agents ?? [];
  const [draft, setDraft] = useDashboardSectionState<Draft>(
    DASHBOARD_SECTION_CACHE_KEYS.operate.scheduleDraft,
    emptyDraft,
  );
  const [busy, setBusy] = useState(false);
  const [actionErr, setActionErr] = useState<string | null>(null);
  const err = actionErr ?? loadErr;

  const refresh = useCallback(async () => {
    setActionErr(null);
    try {
      await refreshSchedules();
    } catch {
      // The section cache stores and exposes the load error.
    }
  }, [refreshSchedules]);

  useEffect(() => {
    if (draft.target_type !== "agent" || draft.agent_name || agents.length === 0) return;
    const agent = agents[0];
    const skillName = agent.card?.skills?.[0]?.name || "";
    setDraft((current) => ({
      ...current,
      agent_name: agent.name,
      skill_name: skillName,
      args_json: defaultArgsJson(skillInputSchema(agent, skillName)),
    }));
  }, [agents, draft.agent_name, draft.target_type]);

  const selectedAgent = useMemo(
    () => agents.find((agent) => agent.name === draft.agent_name) || null,
    [agents, draft.agent_name],
  );
  const scheduleStats = useMemo(() => {
    if (!schedules) return null;
    return schedulePostureStats(schedules);
  }, [schedules]);
  const selectedSchedule =
    view.id === "list" && routeScheduleId
      ? schedules?.find((schedule) => schedule.schedule_id === routeScheduleId) || null
      : null;

  async function submit(): Promise<boolean> {
    if (busy) return false;
    setBusy(true);
    setActionErr(null);
    try {
      const payload: AgentScheduleInput =
        draft.target_type === "main_agent"
          ? {
              name: draft.name,
              enabled: draft.enabled,
              cron: draft.cron,
              timezone: draft.timezone,
              target_type: "main_agent",
              prompt: draft.prompt,
            }
          : {
              name: draft.name,
              enabled: draft.enabled,
              cron: draft.cron,
              timezone: draft.timezone,
              target_type: "agent",
              agent_name: draft.agent_name,
              skill_name: draft.skill_name,
              args_json: draft.args_json,
            };
      const created = await createSchedule(payload);
      setScheduleData((current) => ({
        schedules: [created, ...(current?.schedules || [])],
        agents: current?.agents || agents,
      }));
      setDraft(emptyDraft());
      return true;
    } catch (ex) {
      setActionErr(ex instanceof Error ? ex.message : String(ex));
      return false;
    } finally {
      setBusy(false);
    }
  }

  async function replaceSchedule(promise: Promise<AgentSchedule>) {
    try {
      const next = await promise;
      setScheduleData((current) =>
        current
          ? {
              ...current,
              schedules: current.schedules.map((item) =>
                item.schedule_id === next.schedule_id ? next : item,
              ),
            }
          : current,
      );
      setActionErr(null);
    } catch (ex) {
      setActionErr(ex instanceof Error ? ex.message : String(ex));
    }
  }

  async function removeSchedule(scheduleId: string) {
    try {
      await deleteSchedule(scheduleId);
      setScheduleData((current) =>
        current
          ? {
              ...current,
              schedules: current.schedules.filter(
                (item) => item.schedule_id !== scheduleId,
              ),
            }
          : current,
      );
      setActionErr(null);
      return true;
    } catch (ex) {
      setActionErr(ex instanceof Error ? ex.message : String(ex));
      return false;
    }
  }

  return (
    <RoutePageShell
      routeId="schedules"
      actions={
        <ToolbarButton onClick={refresh} disabled={busy}>
          Refresh
        </ToolbarButton>
      }
    >
      {err && <InlineAlert tone="red">{err}</InlineAlert>}

      <SchedulePosture schedules={schedules} stats={scheduleStats} />

      <PersistentRoutePanel active={view.id === "overview"}>
        <ScheduleOverview schedules={schedules} stats={scheduleStats} />
      </PersistentRoutePanel>

      {/* The schedules list stays mounted behind both the detail sheet and the
          create sheet (mandate C) — neither flow unmounts the list. */}
      <PersistentRoutePanel active={view.id === "list" || view.id === "create"}>
        <ScheduleList
          schedules={schedules}
          onToggle={(schedule) =>
            replaceSchedule(
              updateSchedule(schedule.schedule_id, {
                enabled: !schedule.enabled,
              }),
            )
          }
          onRun={(schedule) => replaceSchedule(runScheduleNow(schedule.schedule_id))}
          onDelete={(schedule) => {
            void removeSchedule(schedule.schedule_id);
          }}
        />
        <ScheduleDetailSheet
          open={view.id === "list" && Boolean(routeScheduleId)}
          onClose={() => navigate("/schedules/list")}
          schedule={selectedSchedule}
          loading={schedules === null}
          requestedScheduleId={routeScheduleId ?? ""}
          initialSection={selectedDetailSection}
          onToggle={(schedule) =>
            replaceSchedule(
              updateSchedule(schedule.schedule_id, {
                enabled: !schedule.enabled,
              }),
            )
          }
          onRun={(schedule) => replaceSchedule(runScheduleNow(schedule.schedule_id))}
          onDelete={async (schedule) => {
            const deleted = await removeSchedule(schedule.schedule_id);
            if (deleted) navigate("/schedules/list");
          }}
        />
        {/* Mandate C: creation opens in place as a right-side sheet layered over
            the list, never a full-page route swap that unmounts the list. */}
        <ScheduleCreateSheet
          open={view.id === "create"}
          onClose={() => navigate("/schedules/list")}
          draft={draft}
          agents={agents}
          selectedAgent={selectedAgent}
          busy={busy}
          onDraft={setDraft}
          onSubmit={async () => {
            const created = await submit();
            if (created) navigate("/schedules/list");
          }}
        />
      </PersistentRoutePanel>
    </RoutePageShell>
  );
}

type ScheduleStats = {
  total: number;
  active: number;
  paused: number;
  attention: number;
  failed: number;
  nextSchedule: AgentSchedule | null;
};

const POSTURE_PILL_TONE: Record<
  "neutral" | "emerald" | "amber" | "red",
  "neutral" | "live" | "authority" | "danger"
> = {
  neutral: "neutral",
  emerald: "live",
  amber: "authority",
  red: "danger",
};

function SchedulePosture({
  schedules,
  stats,
}: {
  schedules: AgentSchedule[] | null;
  stats: ScheduleStats | null;
}) {
  const nextAction = scheduleNextAction(schedules, stats);
  const tone = schedulePostureTone(stats);
  const label = schedulePostureLabel(stats);

  return (
    <DashboardSurfacePosture
      data-onboarding-target="schedules-posture"
      eyebrow="Schedule operations"
      title={schedulePostureDetail(stats)}
      status={{
        label,
        tone: POSTURE_PILL_TONE[tone],
        dot: tone === "emerald",
        pulse: tone === "emerald",
      }}
      metrics={[
        {
          label: "schedules",
          value: stats ? stats.total.toLocaleString() : "...",
        },
        {
          label: "active",
          value: stats ? stats.active.toLocaleString() : "...",
          tone: stats && stats.active > 0 ? "live" : "neutral",
        },
        {
          label: "attention",
          value: stats ? stats.attention.toLocaleString() : "...",
          tone: stats && stats.attention > 0 ? "danger" : "neutral",
        },
        {
          label: "next run",
          value: stats?.nextSchedule ? formatDate(stats.nextSchedule.next_run_at) : "None",
          tone: stats?.nextSchedule ? "live" : "neutral",
        },
      ]}
      actions={
        <>
          <ToolbarLink
            href={nextAction.href}
            variant={nextAction.tone === "primary" ? "primary" : "secondary"}
          >
            {nextAction.action}
          </ToolbarLink>
          <ToolbarLink href="/schedules/list">Schedules</ToolbarLink>
          <ToolbarLink href="/schedules/create" variant="primary">
            Create
          </ToolbarLink>
        </>
      }
    />
  );
}

function ScheduleOverview({
  schedules,
  stats,
}: {
  schedules: AgentSchedule[] | null;
  stats: ScheduleStats | null;
}) {
  const nextAction = scheduleNextAction(schedules, stats);
  return (
    <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_minmax(300px,380px)]">
      <div className="grid gap-3 md:grid-cols-2">
        <ScheduleOverviewLink
          href="/schedules/list"
          label="Existing schedules"
          value={stats ? stats.total.toLocaleString() : "..."}
          detail={stats ? `${stats.active} active, ${stats.attention} need attention` : "loading"}
          tone={stats && stats.attention > 0 ? "amber" : stats && stats.active > 0 ? "emerald" : "neutral"}
        />
        <ScheduleOverviewLink
          href="/schedules/create"
          label="Create"
          value="New schedule"
          detail="Set cadence, target, and payload."
          tone="neutral"
        />
        <ScheduleOverviewLink
          href="/schedules/list"
          label="Paused"
          value={stats ? stats.paused.toLocaleString() : "..."}
          detail="disabled recurring runs"
          tone={stats && stats.paused > 0 ? "amber" : "neutral"}
        />
        <ScheduleOverviewLink
          href={stats?.nextSchedule ? scheduleDetailRoute(stats.nextSchedule.schedule_id) : "/schedules/list"}
          label="Next run"
          value={stats?.nextSchedule ? formatDate(stats.nextSchedule.next_run_at) : "None"}
          detail={stats?.nextSchedule?.name || "no queued active schedules"}
          tone={stats?.nextSchedule ? "emerald" : "neutral"}
        />
      </div>

      <aside className="min-w-0">
        <div className="text-[10px] font-medium uppercase tracking-wide text-ink-faint">
          Operations path
        </div>
        <h2 className="mt-2 text-base font-semibold text-ink">
          Keep scheduled work healthy
        </h2>
        <div className="mt-4 grid gap-2">
          <ScheduleActionLink
            href={nextAction.href}
            label={nextAction.label}
            detail={nextAction.detail}
            tone={nextAction.tone === "primary" ? "amber" : schedulePostureTone(stats)}
          />
          <ScheduleActionLink
            href="/schedules/list"
            label="Review recurring runs"
            detail={stats ? `${stats.total} total, ${stats.active} enabled, ${stats.paused} paused.` : "Loading schedule inventory."}
            tone={stats && stats.active > 0 ? "emerald" : "neutral"}
          />
          <ScheduleActionLink
            href="/schedules/create"
            label="Create a new run"
            detail="Use the builder to pick the target, cadence, and payload."
          />
        </div>
        {stats && stats.attention > 0 && (
          <InlineAlert tone="amber" className="mt-4">
            {stats.attention} schedule{stats.attention === 1 ? "" : "s"} need attention.
          </InlineAlert>
        )}
      </aside>
    </div>
  );
}

function ScheduleOverviewLink({
  href,
  label,
  value,
  detail,
  tone = "neutral",
}: {
  href: string;
  label: string;
  value: string;
  detail: string;
  tone?: "neutral" | "emerald" | "amber" | "red";
}) {
  return (
    <SelectableSurfaceLink href={href} className="p-4">
      <div className="flex min-w-0 items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="text-[10px] uppercase tracking-wider text-ink-faint">
            {label}
          </div>
          <div className="mt-2 truncate text-lg font-semibold text-ink">
            {value}
          </div>
        </div>
        <StatusBadge tone={tone} className="shrink-0">
          Open
        </StatusBadge>
      </div>
      <div className="mt-1 truncate text-xs text-ink-muted">
        {detail}
      </div>
    </SelectableSurfaceLink>
  );
}

function ScheduleActionLink({
  href,
  label,
  detail,
  tone = "neutral",
}: {
  href: string;
  label: string;
  detail: string;
  tone?: "neutral" | "emerald" | "amber" | "red";
}) {
  const dotClassName =
    tone === "emerald"
      ? "bg-signal-live"
      : tone === "amber"
        ? "bg-signal-authority"
        : tone === "red"
          ? "bg-signal-danger"
          : "bg-ink-faint";

  return (
    <SelectableSurfaceLink href={href} className="p-3">
      <div className="flex items-start gap-3">
        <span
          aria-hidden="true"
          className={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${dotClassName}`}
        />
        <span className="min-w-0">
          <span className="block truncate text-sm font-medium text-ink">
            {label}
          </span>
          <span className="mt-1 block text-xs leading-relaxed text-ink-muted">
            {detail}
          </span>
        </span>
      </div>
    </SelectableSurfaceLink>
  );
}

function ScheduleList({
  schedules,
  onToggle,
  onRun,
  onDelete,
}: {
  schedules: AgentSchedule[] | null;
  onToggle: (schedule: AgentSchedule) => void;
  onRun: (schedule: AgentSchedule) => void;
  onDelete: (schedule: AgentSchedule) => void;
}) {
  return (
    <div data-onboarding-target="schedules-list">
      {schedules === null ? (
        <LoadingState label="Loading schedules..." />
      ) : schedules.length === 0 ? (
        <EmptyState title="No schedules" />
      ) : (
        <div className="grid gap-3">
          {schedules.map((schedule) => (
            <ScheduleRow
              key={schedule.schedule_id}
              schedule={schedule}
              onToggle={() => onToggle(schedule)}
              onRun={() => onRun(schedule)}
              onDelete={() => onDelete(schedule)}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function ScheduleCreateSheet({
  open,
  onClose,
  draft,
  agents,
  selectedAgent,
  busy,
  onDraft,
  onSubmit,
}: {
  open: boolean;
  onClose: () => void;
  draft: Draft;
  agents: AgentListing[];
  selectedAgent: AgentListing | null;
  busy: boolean;
  onDraft: Dispatch<SetStateAction<Draft>>;
  onSubmit: () => void;
}) {
  const selectedSkills = selectedAgent?.card?.skills || [];
  const selectedSkill = selectedSkills.find((skill) => skill.name === draft.skill_name);
  const argsSchema = useMemo(
    () => skillInputSchema(selectedAgent, draft.skill_name),
    [selectedAgent, draft.skill_name],
  );
  const argsValue = useMemo(() => parseArgs(draft.args_json), [draft.args_json]);
  const argsFieldCount = skillFieldCount(argsSchema);

  function selectAgent(agent: AgentListing) {
    const skillName = agent.card?.skills?.[0]?.name || "";
    onDraft((current) => ({
      ...current,
      agent_name: agent.name,
      skill_name: skillName,
      args_json: defaultArgsJson(skillInputSchema(agent, skillName)),
    }));
  }

  function selectSkill(skillName: string) {
    onDraft((current) => ({
      ...current,
      skill_name: skillName,
      args_json: defaultArgsJson(skillInputSchema(selectedAgent, skillName)),
    }));
  }

  return (
    <DetailSheet
      open={open}
      onClose={onClose}
      size="lg"
      title="Create schedule"
      description="Define the cadence, target, and payload for a recurring run."
      footer={
        <ToolbarButton
          variant="primary"
          size="md"
          onClick={onSubmit}
          disabled={busy}
        >
          {busy ? "Creating..." : "Create schedule"}
        </ToolbarButton>
      }
    >
      <div data-onboarding-target="schedules-form" className="space-y-4">
          <div className="grid gap-3 sm:grid-cols-2">
            <FormField label="Name">
              <TextInput
                value={draft.name}
                onChange={(event) =>
                  onDraft((current) => ({ ...current, name: event.target.value }))
                }
              />
            </FormField>
            <FormField label="Cron">
              <TextInput
                value={draft.cron}
                onChange={(event) =>
                  onDraft((current) => ({ ...current, cron: event.target.value }))
                }
                placeholder={DEFAULT_CRON}
                mono
              />
            </FormField>
            <FormField label="Timezone">
              <TextInput
                value={draft.timezone}
                onChange={(event) =>
                  onDraft((current) => ({ ...current, timezone: event.target.value }))
                }
                mono
              />
            </FormField>
            <div className="space-y-1">
              <div className="text-xs font-medium text-ink-dim">
                State
              </div>
              <ToggleField
                label="Active"
                checked={draft.enabled}
                onCheckedChange={(enabled) =>
                  onDraft((current) => ({ ...current, enabled }))
                }
                className="h-9 bg-runtime-bg"
              />
            </div>
          </div>

          <SegmentedControl aria-label="Schedule target" className="flex w-full">
            <SegmentedButton
              onClick={() =>
                onDraft((current) => ({ ...current, target_type: "main_agent" }))
              }
              selected={draft.target_type === "main_agent"}
              className="flex-1"
            >
              Main agent
            </SegmentedButton>
            <SegmentedButton
              onClick={() =>
                onDraft((current) => {
                  const agent = agents.find((item) => item.name === current.agent_name)
                    || agents[0];
                  const skillName = agent?.card?.skills?.[0]?.name || "";
                  return {
                    ...current,
                    target_type: "agent",
                    agent_name: agent?.name || "",
                    skill_name: skillName,
                    args_json: defaultArgsJson(skillInputSchema(agent, skillName)),
                  };
                })
              }
              selected={draft.target_type === "agent"}
              className="flex-1"
            >
              Pick agent
            </SegmentedButton>
          </SegmentedControl>

          {draft.target_type === "main_agent" ? (
            <FormField label="Prompt">
              <TextArea
                value={draft.prompt}
                onChange={(event) =>
                  onDraft((current) => ({ ...current, prompt: event.target.value }))
                }
                rows={6}
                className="min-h-[144px]"
              />
            </FormField>
          ) : (
            <div className="space-y-3">
              <div className="grid gap-3 sm:grid-cols-2">
                <FormField label="Agent">
                  <AgentPicker
                    agents={agents}
                    value={draft.agent_name}
                    disabled={busy}
                    onSelect={selectAgent}
                  />
                </FormField>
                <FormField
                  label="Tool"
                  description={selectedSkill?.description || undefined}
                >
                  <SelectInput
                    value={draft.skill_name}
                    disabled={busy || selectedSkills.length === 0}
                    onChange={(event) => selectSkill(event.target.value)}
                  >
                    {selectedSkills.length === 0 && (
                      <option value="">No tools available</option>
                    )}
                    {selectedSkills.map((skill) => (
                      <option key={skill.name} value={skill.name}>
                        {skill.name}
                      </option>
                    ))}
                  </SelectInput>
                </FormField>
              </div>
              <div>
                <div className="mb-1 text-xs font-medium text-ink-dim">Arguments</div>
                {argsFieldCount > 0 ? (
                  <ChatStructuredInputPanel
                    schema={argsSchema}
                    requestId={`${draft.agent_name}:${draft.skill_name}`}
                    value={argsValue}
                    showHeader={false}
                    disabled={busy}
                    className="bg-runtime-bg/60"
                    onChange={(next) =>
                      onDraft((current) => ({
                        ...current,
                        args_json: JSON.stringify(next, null, 2),
                      }))
                    }
                  />
                ) : (
                  <InlineAlert tone="neutral">
                    {draft.skill_name
                      ? "This tool takes no arguments — nothing else to configure."
                      : "Pick an agent and tool to configure its arguments."}
                  </InlineAlert>
                )}
              </div>
            </div>
          )}

        <SurfacePanel
          as="div"
          className="flex flex-wrap items-center justify-between gap-3 bg-runtime-panel/40 p-4"
        >
          <div className="min-w-0">
            <div className="text-xs uppercase text-ink-muted">
              Target
            </div>
            <div className="mt-2 text-sm font-medium text-ink">
              {draft.target_type === "main_agent"
                ? "Main orchestrator"
                : draft.agent_name
                  ? `${draft.agent_name}.${draft.skill_name || "skill"}`
                  : "No agent selected"}
            </div>
          </div>
          <div className="font-mono text-xs text-ink-muted">
            {draft.cron || DEFAULT_CRON} | {draft.timezone || "UTC"}
          </div>
        </SurfacePanel>
      </div>
    </DetailSheet>
  );
}

// Searchable agent combobox. Expands inline (not an absolute popover) so it is
// never clipped by the create sheet's scroll container.
function AgentPicker({
  agents,
  value,
  disabled,
  onSelect,
}: {
  agents: AgentListing[];
  value: string;
  disabled?: boolean;
  onSelect: (agent: AgentListing) => void;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [activeIndex, setActiveIndex] = useState(0);
  const selected = agents.find((agent) => agent.name === value) || null;

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return agents;
    return agents.filter((agent) =>
      [agent.name, agent.description, ...(agent.card?.skills ?? []).map((skill) => skill.name)]
        .filter(Boolean)
        .join(" ")
        .toLowerCase()
        .includes(q),
    );
  }, [agents, query]);

  useEffect(() => {
    setActiveIndex(0);
  }, [query, open]);

  function choose(agent: AgentListing | undefined) {
    if (!agent) return;
    onSelect(agent);
    setOpen(false);
    setQuery("");
  }

  if (agents.length === 0) {
    return (
      <InlineAlert tone="amber">
        No agents available. Deploy or import an agent first.
      </InlineAlert>
    );
  }

  return (
    <div>
      <button
        type="button"
        disabled={disabled}
        onClick={() => setOpen((current) => !current)}
        aria-haspopup="listbox"
        aria-expanded={open}
        className="flex w-full items-center justify-between gap-3 rounded-md border border-runtime-line-soft/60 bg-runtime-bg px-3 py-2 text-left text-sm transition hover:border-runtime-line-mid disabled:cursor-not-allowed disabled:opacity-50"
      >
        <span className="min-w-0">
          {selected ? (
            <>
              <span className="block truncate font-medium text-ink">{selected.name}</span>
              {selected.description && (
                <span className="mt-0.5 block truncate text-xs text-ink-muted">
                  {selected.description}
                </span>
              )}
            </>
          ) : (
            <span className="text-ink-muted">Select an agent</span>
          )}
        </span>
        <span aria-hidden="true" className="shrink-0 text-[10px] text-ink-faint">
          {open ? "▲" : "▼"}
        </span>
      </button>

      {open && (
        <div className="mt-1 overflow-hidden rounded-md border border-runtime-line-soft/60 bg-runtime-panel">
          <div className="border-b border-runtime-line-soft/60 p-2">
            <TextInput
              autoFocus
              compact
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "ArrowDown") {
                  event.preventDefault();
                  setActiveIndex((index) => Math.min(index + 1, filtered.length - 1));
                } else if (event.key === "ArrowUp") {
                  event.preventDefault();
                  setActiveIndex((index) => Math.max(index - 1, 0));
                } else if (event.key === "Enter") {
                  event.preventDefault();
                  choose(filtered[activeIndex]);
                } else if (event.key === "Escape") {
                  event.preventDefault();
                  setOpen(false);
                }
              }}
              placeholder="Search agents..."
            />
          </div>
          <ul role="listbox" className="max-h-64 overflow-y-auto p-1">
            {filtered.length === 0 ? (
              <li className="px-3 py-4 text-center text-xs text-ink-muted">
                No agents match "{query}".
              </li>
            ) : (
              filtered.map((agent, index) => (
                <li key={agent.id}>
                  <button
                    type="button"
                    role="option"
                    aria-selected={agent.name === value}
                    onMouseEnter={() => setActiveIndex(index)}
                    onClick={() => choose(agent)}
                    className={`flex w-full flex-col gap-0.5 rounded px-3 py-2 text-left transition ${
                      index === activeIndex ? "bg-runtime-bg" : "hover:bg-runtime-bg/60"
                    } ${agent.name === value ? "ring-1 ring-signal-protocol/40" : ""}`}
                  >
                    <span className="flex items-center justify-between gap-2">
                      <span className="truncate text-sm font-medium text-ink">
                        {agent.name}
                      </span>
                      <span className="shrink-0 text-[10px] uppercase tracking-wide text-ink-faint">
                        {agent.card?.skills?.length ?? 0} skill
                        {(agent.card?.skills?.length ?? 0) === 1 ? "" : "s"}
                      </span>
                    </span>
                    {agent.description && (
                      <span className="truncate text-xs text-ink-muted">
                        {agent.description}
                      </span>
                    )}
                  </button>
                </li>
              ))
            )}
          </ul>
        </div>
      )}
    </div>
  );
}

function ScheduleRow({
  schedule,
  onToggle,
  onRun,
  onDelete,
}: {
  schedule: AgentSchedule;
  onToggle: () => void;
  onRun: () => void;
  onDelete: () => void;
}) {
  const target = scheduleTargetLabel(schedule);
  const failed = Boolean(schedule.last_error) || scheduleHasFailed(schedule);
  return (
    <SurfacePanel
      as="article"
      className={`bg-runtime-bg/70 p-4 ${
        failed ? "border-signal-danger/50 ring-signal-danger/10" : ""
      }`}
    >
      <div className="flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="truncate text-sm font-semibold text-ink">
              {schedule.name}
            </h2>
            <StatusBadge tone={schedule.enabled ? "emerald" : "neutral"} dot>
              {schedule.enabled ? "Enabled" : "Paused"}
            </StatusBadge>
            {schedule.last_run_status && (
              <StateBadge status={schedule.last_run_status} />
            )}
            {failed && <StatusBadge tone="red">attention</StatusBadge>}
          </div>
          <div className="mt-2 grid gap-2 text-xs text-ink-muted sm:grid-cols-2 lg:grid-cols-4">
            <SummaryMetric
              label="Target"
              value={target}
              size="compact"
              mono={false}
            />
            <SummaryMetric
              label="Cron"
              value={`${schedule.cron} ${schedule.timezone}`}
              size="compact"
            />
            <SummaryMetric
              label="Next"
              value={formatDate(schedule.next_run_at)}
              size="compact"
              mono={false}
            />
            <SummaryMetric
              label="Last"
              value={formatDate(schedule.last_run_at)}
              size="compact"
              mono={false}
            />
          </div>
          {schedule.target_type === "main_agent" && schedule.prompt && (
            <p className="mt-3 line-clamp-2 text-sm text-ink-dim">
              {schedule.prompt}
            </p>
          )}
          {schedule.last_error && (
            <div className="mt-3">
              <InlineAlert tone="red">{schedule.last_error}</InlineAlert>
            </div>
          )}
        </div>
        <div className="flex shrink-0 flex-wrap gap-2">
          <ToolbarLink href={scheduleDetailRoute(schedule.schedule_id)}>
            Open
          </ToolbarLink>
          <ToolbarButton onClick={onRun}>Run now</ToolbarButton>
          <ToolbarButton onClick={onToggle}>
            {schedule.enabled ? "Pause" : "Enable"}
          </ToolbarButton>
          <ToolbarButton variant="danger" onClick={onDelete}>
            Delete
          </ToolbarButton>
        </div>
      </div>
    </SurfacePanel>
  );
}

function ScheduleDetailSheet({
  open,
  onClose,
  schedule,
  loading,
  requestedScheduleId,
  initialSection,
  onToggle,
  onRun,
  onDelete,
}: {
  open: boolean;
  onClose: () => void;
  schedule: AgentSchedule | null;
  loading: boolean;
  requestedScheduleId: string;
  initialSection: ScheduleDetailSectionId;
  onToggle: (schedule: AgentSchedule) => void;
  onRun: (schedule: AgentSchedule) => void;
  onDelete: (schedule: AgentSchedule) => void | Promise<void>;
}) {
  // Detail tabs live in local state inside the sheet body (mandate D): no route
  // params. Deep links to .../:scheduleId/:section still hydrate the starting tab.
  const [section, setSection] = useState<ScheduleDetailSectionId>(initialSection);
  useEffect(() => {
    if (open) setSection(initialSection);
  }, [open, initialSection, requestedScheduleId]);

  const failed =
    Boolean(schedule?.last_error) || (schedule ? scheduleHasFailed(schedule) : false);
  const title = loading
    ? "Loading schedule..."
    : schedule
      ? schedule.name
      : "Schedule not found";

  const footer =
    schedule && !loading ? (
      <div className="flex flex-wrap gap-2">
        <ToolbarButton onClick={() => onRun(schedule)}>Run now</ToolbarButton>
        <ToolbarButton onClick={() => onToggle(schedule)}>
          {schedule.enabled ? "Pause" : "Enable"}
        </ToolbarButton>
        <ToolbarButton variant="danger" onClick={() => onDelete(schedule)}>
          Delete
        </ToolbarButton>
      </div>
    ) : undefined;

  return (
    <DetailSheet
      open={open}
      onClose={onClose}
      size="lg"
      title={title}
      description={
        schedule && !loading ? (
          <span className="break-all font-mono text-[11px] text-ink-faint">
            {schedule.schedule_id}
          </span>
        ) : undefined
      }
      footer={footer}
    >
      <ScheduleDetail
        schedule={schedule}
        loading={loading}
        requestedScheduleId={requestedScheduleId}
        section={section}
        onSelectSection={setSection}
        failed={failed}
        onClose={onClose}
      />
    </DetailSheet>
  );
}

function ScheduleDetail({
  schedule,
  loading,
  requestedScheduleId,
  section,
  onSelectSection,
  failed,
  onClose,
}: {
  schedule: AgentSchedule | null;
  loading: boolean;
  requestedScheduleId: string;
  section: ScheduleDetailSectionId;
  onSelectSection: (section: ScheduleDetailSectionId) => void;
  failed: boolean;
  onClose: () => void;
}) {
  if (loading) return <LoadingState label="Loading schedule..." />;
  if (!schedule) {
    return (
      <EmptyState
        title="Schedule not found"
        description={`${requestedScheduleId} is not in the current schedule list.`}
        action={
          <ToolbarButton onClick={onClose}>Back to schedules</ToolbarButton>
        }
      />
    );
  }

  const target = scheduleTargetLabel(schedule);
  const isAgentTarget = schedule.target_type === "agent";

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <StatusBadge tone={schedule.enabled ? "emerald" : "neutral"} dot>
          {schedule.enabled ? "Enabled" : "Paused"}
        </StatusBadge>
        {schedule.last_run_status && <StateBadge status={schedule.last_run_status} />}
        {failed && <StatusBadge tone="red">attention</StatusBadge>}
      </div>

      {schedule.last_error && (
        <InlineAlert tone="red" role="alert">
          {schedule.last_error}
        </InlineAlert>
      )}

      <ScheduleDetailNav
        activeSection={section}
        onSelectSection={onSelectSection}
      />

      {section === "payload" ? (
        <SchedulePayloadPanel schedule={schedule} isAgentTarget={isAgentTarget} />
      ) : section === "metadata" ? (
        <ScheduleMetadataPanel schedule={schedule} />
      ) : section === "execution" ? (
        <ScheduleExecutionPanel schedule={schedule} />
      ) : (
        <ScheduleOverviewPanel schedule={schedule} target={target} />
      )}
    </div>
  );
}

function ScheduleDetailNav({
  activeSection,
  onSelectSection,
}: {
  activeSection: ScheduleDetailSectionId;
  onSelectSection: (section: ScheduleDetailSectionId) => void;
}) {
  return (
    <SegmentedControl
      role="tablist"
      aria-label="Schedule detail sections"
      className="flex gap-1 overflow-x-auto bg-runtime-panel/60"
    >
      {SCHEDULE_DETAIL_SECTIONS.map((item) => (
        <SegmentedButton
          key={item.id}
          onClick={() => onSelectSection(item.id)}
          selected={activeSection === item.id}
          className="whitespace-nowrap"
        >
          {item.label}
        </SegmentedButton>
      ))}
    </SegmentedControl>
  );
}

function ScheduleOverviewPanel({
  schedule,
  target,
}: {
  schedule: AgentSchedule;
  target: string;
}) {
  return (
    <SurfacePanel as="section" className="bg-runtime-bg p-4">
      <div className="mb-3">
        <div className="text-xs uppercase text-ink-muted">Overview</div>
        <p className="mt-1 text-sm leading-relaxed text-ink-muted">
          Cadence, target, lifecycle dates, and run count for this saved schedule.
        </p>
      </div>
      <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
        <SummaryMetric label="target" value={target} size="compact" mono={false} />
        <SummaryMetric label="cron" value={schedule.cron} size="compact" />
        <SummaryMetric label="timezone" value={schedule.timezone} size="compact" />
        <SummaryMetric label="runs" value={schedule.run_count} size="compact" />
        <SummaryMetric
          label="next run"
          value={formatDate(schedule.next_run_at)}
          size="compact"
          mono={false}
        />
        <SummaryMetric
          label="last run"
          value={formatDate(schedule.last_run_at)}
          size="compact"
          mono={false}
        />
        <SummaryMetric
          label="created"
          value={formatDate(schedule.created_at)}
          size="compact"
          mono={false}
        />
        <SummaryMetric
          label="updated"
          value={formatDate(schedule.updated_at)}
          size="compact"
          mono={false}
        />
      </div>
    </SurfacePanel>
  );
}

function SchedulePayloadPanel({
  schedule,
  isAgentTarget,
}: {
  schedule: AgentSchedule;
  isAgentTarget: boolean;
}) {
  return (
    <SurfacePanel as="section" className="bg-runtime-bg p-4">
      <div className="mb-3">
        <div className="text-xs uppercase text-ink-muted">Payload</div>
        <p className="mt-1 text-sm leading-relaxed text-ink-muted">
          The exact prompt or agent arguments sent when this schedule runs.
        </p>
      </div>
      {isAgentTarget ? (
        <CodeBlock className="max-h-[520px] text-xs">
          {schedule.args_json || "{}"}
        </CodeBlock>
      ) : (
        <div className="whitespace-pre-wrap rounded-md border border-runtime-line-soft/60 bg-runtime-panel/50 p-3 text-sm leading-relaxed text-ink-soft">
          {schedule.prompt || "No prompt configured."}
        </div>
      )}
    </SurfacePanel>
  );
}

function ScheduleMetadataPanel({ schedule }: { schedule: AgentSchedule }) {
  return (
    <SurfacePanel as="section" className="bg-runtime-bg p-4">
      <div className="mb-3">
        <div className="text-xs uppercase text-ink-muted">Metadata</div>
        <p className="mt-1 text-sm leading-relaxed text-ink-muted">
          Control-plane metadata stored with this schedule.
        </p>
      </div>
      <CodeBlock className="max-h-[520px] text-xs">
        {JSON.stringify(schedule.metadata || {}, null, 2)}
      </CodeBlock>
    </SurfacePanel>
  );
}

function ScheduleExecutionPanel({ schedule }: { schedule: AgentSchedule }) {
  return (
    <SurfacePanel as="section" className="bg-runtime-bg p-4">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <div className="text-xs uppercase text-ink-muted">Last execution</div>
          <div className="mt-2 text-sm font-medium text-ink">
            {schedule.last_run_status || "No run yet"}
          </div>
          <div className="mt-1 text-xs text-ink-muted">
            {formatDate(schedule.last_run_at)}
          </div>
        </div>
        {schedule.last_run_job_id && (
          <ToolbarLink
            href={`/activity/${encodeURIComponent(schedule.last_run_job_id)}`}
            variant="primary"
          >
            Open job
          </ToolbarLink>
        )}
      </div>
      {!schedule.last_run_job_id && (
        <InlineAlert tone="amber" className="mt-4 text-xs">
          This schedule has not produced a job yet.
        </InlineAlert>
      )}
      <div className="mt-4 grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
        <SummaryMetric
          label="status"
          value={schedule.last_run_status || "not run"}
          size="compact"
        />
        <SummaryMetric
          label="last run"
          value={formatDate(schedule.last_run_at)}
          size="compact"
          mono={false}
        />
        <SummaryMetric
          label="job id"
          value={schedule.last_run_job_id || "-"}
          size="compact"
        />
        <SummaryMetric
          label="run count"
          value={schedule.run_count}
          size="compact"
        />
        <SummaryMetric label="agent" value={schedule.agent_name || "-"} size="compact" />
        <SummaryMetric label="tool" value={schedule.skill_name || "-"} size="compact" />
      </div>
    </SurfacePanel>
  );
}

function scheduleTargetLabel(schedule: AgentSchedule) {
  return schedule.target_type === "main_agent"
    ? "Main orchestrator"
    : `${schedule.agent_name || "agent"}.${schedule.skill_name || "skill"}`;
}

function schedulePostureStats(schedules: AgentSchedule[]): ScheduleStats {
  const active = schedules.filter((schedule) => schedule.enabled).length;
  const failed = schedules.filter((schedule) => scheduleHasFailed(schedule)).length;
  const attention = schedules.filter((schedule) =>
    Boolean(schedule.last_error) || scheduleHasFailed(schedule),
  ).length;
  const nextSchedule = schedules
    .filter((schedule) => schedule.enabled && schedule.next_run_at)
    .sort(
      (left, right) =>
        new Date(left.next_run_at || "").getTime() -
        new Date(right.next_run_at || "").getTime(),
    )[0] || null;

  return {
    total: schedules.length,
    active,
    paused: schedules.length - active,
    attention,
    failed,
    nextSchedule,
  };
}

function scheduleNextAction(
  schedules: AgentSchedule[] | null,
  stats: ScheduleStats | null,
) {
  if (!schedules || !stats) {
    return {
      label: "Load schedule inventory",
      detail: "Checking recurring runs, latest execution state, and queued cadence.",
      href: "/schedules/list",
      action: "Schedules",
      tone: "secondary" as const,
    };
  }
  if (stats.total === 0) {
    return {
      label: "Create the first schedule",
      detail: "Set up a recurring orchestrator or agent run with a saved cadence and payload.",
      href: "/schedules/create",
      action: "Create schedule",
      tone: "primary" as const,
    };
  }
  const errorSchedule = schedules.find((schedule) =>
    Boolean(schedule.last_error) || scheduleHasFailed(schedule),
  );
  if (errorSchedule) {
    return {
      label: "Review failed schedule",
      detail: errorSchedule.last_error || `${errorSchedule.name} last reported ${errorSchedule.last_run_status}.`,
      href: scheduleDetailRoute(errorSchedule.schedule_id),
      action: "Review failure",
      tone: "primary" as const,
    };
  }
  if (stats.active === 0) {
    return {
      label: "Enable a recurring run",
      detail: "All schedules are paused, so no saved work is currently queued.",
      href: "/schedules/list",
      action: "Review paused",
      tone: "secondary" as const,
    };
  }
  if (stats.nextSchedule) {
    return {
      label: "Inspect the next queued run",
      detail: `${stats.nextSchedule.name} is next at ${formatDate(stats.nextSchedule.next_run_at)}.`,
      href: scheduleDetailRoute(stats.nextSchedule.schedule_id),
      action: "Open next",
      tone: "secondary" as const,
    };
  }
  return {
    label: "Review schedule cadence",
    detail: "Active schedules exist, but none currently report a next run time.",
    href: "/schedules/list",
    action: "Review list",
    tone: "secondary" as const,
  };
}

function schedulePostureTone(stats: ScheduleStats | null) {
  if (!stats) return "neutral" as const;
  if (stats.attention > 0) return "red" as const;
  if (stats.active > 0) return "emerald" as const;
  if (stats.total > 0) return "amber" as const;
  return "neutral" as const;
}

function schedulePostureLabel(stats: ScheduleStats | null) {
  if (!stats) return "checking";
  if (stats.attention > 0) return "needs attention";
  if (stats.active > 0) return "active";
  if (stats.total > 0) return "paused";
  return "empty";
}

function schedulePostureDetail(stats: ScheduleStats | null) {
  if (!stats) return "loading schedule inventory";
  if (stats.attention > 0) {
    return `${stats.attention} schedule${stats.attention === 1 ? "" : "s"} need review`;
  }
  if (stats.active > 0) {
    return `${stats.active} active recurring run${stats.active === 1 ? "" : "s"}`;
  }
  if (stats.total > 0) return "all saved schedules are paused";
  return "no recurring runs configured yet";
}

function scheduleHasFailed(schedule: AgentSchedule) {
  const status = String(schedule.last_run_status || "").toLowerCase();
  return ["failed", "error", "cancelled", "canceled"].includes(status);
}

function formatDate(value: string | null) {
  if (!value) return "Never";
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(value));
}
