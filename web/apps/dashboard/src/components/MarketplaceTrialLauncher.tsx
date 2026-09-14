import { useCallback, useEffect, useMemo, useState } from "react";
import type { FormEvent } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import {
  createTrialRoom,
  getConsumerSetup,
  listFiles,
  runTrialAgent,
  upsertConsumerSetup,
  upsertOrgConsumerSetup,
  type AgentListing,
  type ConsumerSetupField,
  type ConsumerSetupStatus,
  type AgentSkill,
  type FileMeta,
  type TrialRoom,
} from "../api";
import { ConsumerSetupFieldInput } from "./ConsumerSetupFieldInput";
import {
  BadgeFact,
  EmptyState,
  FormField,
  InlineAlert,
  LoadingState,
  SelectInput,
  SurfacePanel,
  TextArea,
  TextInput,
  ToolbarButton,
  ToolbarLink,
} from "./DashboardChrome";
import { StatusBadge } from "./StatusPillAdapters";

type LaunchStage = "idle" | "creating" | "running" | "success";

type MarketplaceTrialIntentInput =
  | string
  | URL
  | URLSearchParams
  | Pick<Location, "search">
  | null
  | undefined;

export type MarketplaceTrialQueryIntent = {
  open: boolean;
  agent: string;
  skill: string;
  goal: string;
  criteria: string;
  files: string[];
  argsJson: string;
};

export type MarketplaceTrialLauncherProps = {
  agent: AgentListing;
  skill?: AgentSkill | string | null;
  onLaunched?: (room: TrialRoom) => void;
  enableUrlIntent?: boolean;
  intentSource?: MarketplaceTrialIntentInput;
  backHref?: string;
};

const DEFAULT_OUTPUT_SCHEMA = {
  type: "object",
  required: ["summary"],
  properties: {
    summary: { type: "string" },
  },
} satisfies Record<string, unknown>;

const URL_INTENT_KEYS = [
  "intent",
  "action",
  "launch",
  "trial",
  "launchTrial",
  "launch_trial",
];

function readMarketplaceTrialIntent(
  input?: MarketplaceTrialIntentInput,
): MarketplaceTrialQueryIntent {
  const params = paramsFromIntentInput(input);
  const rawIntentValues = URL_INTENT_KEYS.map((key) => params.get(key)).filter(
    (value): value is string => Boolean(value),
  );

  return {
    open: rawIntentValues.some(isTrialIntentValue),
    agent: firstParam(params, ["agent", "agent_name", "trialAgent", "trial_agent"]),
    skill: firstParam(params, ["skill", "skill_name", "trialSkill", "trial_skill"]),
    goal: firstParam(params, ["goal", "trial_goal"]),
    criteria: firstParam(params, [
      "criteria",
      "acceptance",
      "acceptance_criteria",
      "trial_criteria",
    ]),
    files: parseWorkspacePaths(
      [
        ...params.getAll("file"),
        ...params.getAll("files"),
        ...params.getAll("path"),
        ...params.getAll("input_path"),
        ...params.getAll("input_paths"),
      ].join("\n"),
    ),
    argsJson: firstParam(params, ["args", "args_json", "trial_args"]),
  };
}

export function MarketplaceTrialLauncher({
  agent,
  skill,
  onLaunched,
  enableUrlIntent = true,
  intentSource,
  backHref,
}: MarketplaceTrialLauncherProps) {
  const location = useLocation();
  const navigate = useNavigate();
  const preferredSkillName = useMemo(
    () => preferredSkillNameFor(agent, skill),
    [agent, skill],
  );
  const [selectedSkillName, setSelectedSkillName] = useState(preferredSkillName);
  const [goal, setGoal] = useState("");
  const [criteria, setCriteria] = useState("");
  const [pathsText, setPathsText] = useState("");
  const [argsJson, setArgsJson] = useState("");
  const [stage, setStage] = useState<LaunchStage>("idle");
  const [err, setErr] = useState<string | null>(null);
  const [createdRoom, setCreatedRoom] = useState<TrialRoom | null>(null);
  const [consumedIntentKey, setConsumedIntentKey] = useState("");
  const [setupStatus, setSetupStatus] = useState<ConsumerSetupStatus | null>(null);
  const [setupValues, setSetupValues] = useState<Record<string, unknown>>({});
  const [setupLoading, setSetupLoading] = useState(false);
  const [setupSaving, setSetupSaving] = useState<"user" | "org" | null>(null);
  const [setupError, setSetupError] = useState<string | null>(null);

  const skills = useMemo(() => agent.card?.skills || [], [agent.card?.skills]);
  const selectedSkill = useMemo(
    () => findSkill(skills, selectedSkillName),
    [selectedSkillName, skills],
  );
  const inputPaths = useMemo(() => parseWorkspacePaths(pathsText), [pathsText]);
  const argsJsonError = useMemo(() => validateOptionalJson(argsJson), [argsJson]);
  const busy = stage === "creating" || stage === "running";
  const blockedReason = marketplaceLaunchBlockReason(agent);
  const setupFields = setupStatus?.declaration.fields || agent.card?.consumer_setup?.fields || [];
  const hasSetup = setupFields.length > 0;
  const setupComplete = !hasSetup || Boolean(setupStatus?.complete);
  const canSubmit =
    !busy &&
    !blockedReason &&
    setupComplete &&
    Boolean(goal.trim()) &&
    !argsJsonError &&
    (skills.length === 0 || Boolean(selectedSkill));
  const intent = useMemo(
    () => readMarketplaceTrialIntent(intentSource ?? location.search),
    [intentSource, location.search],
  );
  const intentKey = useMemo(() => intentSignature(intent), [intent]);

  useEffect(() => {
    setSelectedSkillName((current) => {
      if (current && findSkill(skills, current)) return current;
      return preferredSkillName;
    });
  }, [preferredSkillName, skills]);

  useEffect(() => {
    setStage("idle");
    setErr(null);
    setCreatedRoom(null);
    setSetupStatus(null);
    setSetupValues({});
    setSetupError(null);
  }, [agent.id]);

  useEffect(() => {
    if (!hasSetup) return;
    let active = true;
    setSetupLoading(true);
    setSetupError(null);
    getConsumerSetup(agent.name)
      .then((status) => {
        if (!active) return;
        setSetupStatus(status);
        setSetupValues(initialSetupValues(status));
      })
      .catch((ex) => {
        if (active) setSetupError(messageFromError(ex));
      })
      .finally(() => {
        if (active) setSetupLoading(false);
      });
    return () => {
      active = false;
    };
  }, [agent.name, hasSetup]);

  useEffect(() => {
    if (!enableUrlIntent || !intent.open || consumedIntentKey === intentKey) return;
    if (!intent.agent || !sameName(intent.agent, agent.name)) return;

    const intentSkill = intent.skill ? findSkill(skills, intent.skill) : null;
    if (intentSkill) setSelectedSkillName(intentSkill.name);
    if (intent.goal) setGoal(intent.goal);
    if (intent.criteria) setCriteria(intent.criteria);
    if (intent.files.length > 0) setPathsText(intent.files.join("\n"));
    if (intent.argsJson) setArgsJson(intent.argsJson);
    setConsumedIntentKey(intentKey);
  }, [
    agent.name,
    consumedIntentKey,
    enableUrlIntent,
    intent,
    intentKey,
    skills,
  ]);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!canSubmit) return;

    setStage("creating");
    setErr(null);
    setCreatedRoom(null);

    let room: TrialRoom | null = null;
    try {
      const trimmedGoal = goal.trim();
      const trimmedCriteria = criteria.trim();
      const trimmedArgs = argsJson.trim();
      if (trimmedArgs) JSON.parse(trimmedArgs);

      room = await createTrialRoom({
        title: trialTitle(agent.name, selectedSkill?.name || selectedSkillName),
        goal: trimmedGoal,
        acceptance_criteria: trimmedCriteria || undefined,
        input_paths: inputPaths,
        output_schema: DEFAULT_OUTPUT_SCHEMA,
        max_runtime_seconds: 300,
      });
      setCreatedRoom(room);
      setStage("running");

      const launchedRoom = await runTrialAgent(room.slug, {
        agent_name: agent.name,
        skill_name: selectedSkill?.name || selectedSkillName || undefined,
        args_json: trimmedArgs || undefined,
      });
      setCreatedRoom(launchedRoom);
      setStage("success");
      onLaunched?.(launchedRoom);
    } catch (ex) {
      setStage("idle");
      setErr(
        room
          ? `Trial room created, but the run failed: ${messageFromError(ex)}`
          : messageFromError(ex),
      );
    }
  }

  function openTrials() {
    const params = new URLSearchParams();
    params.set("agent", agent.name);
    if (selectedSkill?.name || selectedSkillName) {
      params.set("skill", selectedSkill?.name || selectedSkillName);
    }
    if (createdRoom?.slug) params.set("room", createdRoom.slug);
    const href = `/trials?${params.toString()}`;
    navigate(href);
  }

  function runAnother() {
    setStage("idle");
    setErr(null);
    setCreatedRoom(null);
  }

  async function saveSetup(scope: "user" | "org") {
    setSetupSaving(scope);
    setSetupError(null);
    try {
      const values = payloadSetupValues(setupFields, setupValues);
      const next =
        scope === "org"
          ? await upsertOrgConsumerSetup(agent.name, values)
          : await upsertConsumerSetup(agent.name, values);
      setSetupStatus(next);
      setSetupValues(initialSetupValues(next));
    } catch (ex) {
      setSetupError(messageFromError(ex));
    } finally {
      setSetupSaving(null);
    }
  }

  const actions = (
    <div className="flex w-full flex-col-reverse gap-2 sm:flex-row sm:justify-end">
      {stage === "success" ? (
        <>
          <ToolbarButton
            type="button"
            onClick={runAnother}
          >
            Run another
          </ToolbarButton>
          <ToolbarButton
            type="button"
            onClick={openTrials}
            variant="primary"
          >
            Open Agent Trials
          </ToolbarButton>
        </>
      ) : (
        <ToolbarButton
          type="submit"
          form="marketplace-trial-launcher-form"
          disabled={!canSubmit}
          aria-busy={busy}
          variant="primary"
        >
          {stage === "creating"
            ? "Creating trial..."
            : stage === "running"
              ? "Running agent..."
              : "Create and run"}
        </ToolbarButton>
      )}
    </div>
  );

  const form = (
    <form
      id="marketplace-trial-launcher-form"
      onSubmit={submit}
      aria-busy={busy}
      className="space-y-5"
    >
          <dl className="grid gap-3 border-b border-runtime-line-soft/60 pb-4 sm:grid-cols-3">
            <BadgeFact label="Agent" value={agent.name} />
            <BadgeFact label="Status" value={agent.status} tone={blockedReason ? "amber" : "emerald"} />
            <BadgeFact
              label="Visibility"
              value={agent.public ? "public" : "private"}
              tone={agent.public ? "emerald" : "amber"}
            />
          </dl>

          {blockedReason && (
            <InlineAlert tone="amber" role="alert">
              <span className="font-medium">Launch blocked: </span>
              {blockedReason}
            </InlineAlert>
          )}

          {hasSetup && (
            <ConsumerSetupPanel
              fields={setupFields}
              status={setupStatus}
              values={setupValues}
              onChange={(name, value) =>
                setSetupValues((current) => ({ ...current, [name]: value }))
              }
              loading={setupLoading}
              saving={setupSaving}
              error={setupError}
              disabled={busy || stage === "success"}
              onSaveUser={() => saveSetup("user")}
              onSaveOrg={() => saveSetup("org")}
            />
          )}

          <fieldset disabled={busy || stage === "success"} className="space-y-4">
            <SkillSelect
              skills={skills}
              value={selectedSkill?.name || selectedSkillName}
              onChange={setSelectedSkillName}
            />
            <FormField label="Goal">
              <TextArea
                value={goal}
                onChange={(event) => setGoal(event.target.value)}
                rows={5}
                required
                placeholder="Describe the exact work this marketplace agent should complete."
              />
            </FormField>
            <FormField label="Acceptance criteria">
              <TextArea
                value={criteria}
                onChange={(event) => setCriteria(event.target.value)}
                rows={4}
                placeholder="List the checks that make the trial pass."
              />
            </FormField>
            <WorkspaceFileSelector
              value={inputPaths}
              onChange={(next) => setPathsText(next.join("\n"))}
              disabled={busy || stage === "success"}
              open
            />
            <FormField label="Workspace file paths">
              <TextArea
                value={pathsText}
                onChange={(event) => setPathsText(event.target.value)}
                rows={3}
                mono
                placeholder={"invoices/may.csv\nreports/context.md"}
              />
            </FormField>
            <FormField label="Args JSON">
              <TextArea
                value={argsJson}
                onChange={(event) => setArgsJson(event.target.value)}
                rows={6}
                mono
                invalid={Boolean(argsJsonError)}
                aria-describedby="marketplace-trial-args-json-state"
                placeholder={'{\n  "priority": "high"\n}'}
              />
            </FormField>
            <div
              id="marketplace-trial-args-json-state"
              role={argsJsonError ? "alert" : undefined}
              className={`text-xs ${argsJsonError ? "text-signal-danger" : "text-ink-faint"}`}
            >
              {argsJsonError || "Leave blank to let the trial room provide scoped args."}
            </div>
          </fieldset>

          {err && (
            <InlineAlert tone="red" role="alert">
              <span className="font-medium">Trial launch failed: </span>
              {err}
            </InlineAlert>
          )}

          <div role="status" aria-live="polite" className="text-sm text-ink-dim">
            {stage === "creating" && "Creating trial room..."}
            {stage === "running" && "Running marketplace agent..."}
            {stage === "success" && createdRoom && `Trial launched in ${createdRoom.title}.`}
          </div>
    </form>
  );

  // Rendered as the body of a right-side DetailSheet (mandate C). The sheet owns
  // the agent-name title and close affordance, so this only emits the intent
  // copy, the launch form, and the launch/back actions.
  return (
    <section className="space-y-4">
      <div className="flex flex-col gap-3 border-b border-runtime-line-soft/60 pb-4 sm:flex-row sm:items-start sm:justify-between">
        <p className="min-w-0 max-w-2xl text-sm leading-relaxed text-ink-muted">
          Create a private Agent Trial room, scope the inputs, and run this marketplace agent against the same evidence shape used for comparisons.
        </p>
        <div className="flex shrink-0 flex-wrap gap-2">
          {backHref && (
            <ToolbarLink href={backHref}>
              Back to card
            </ToolbarLink>
          )}
          {actions}
        </div>
      </div>
      {form}
    </section>
  );
}

function ConsumerSetupPanel({
  fields,
  status,
  values,
  onChange,
  loading,
  saving,
  error,
  disabled,
  onSaveUser,
  onSaveOrg,
}: {
  fields: ConsumerSetupField[];
  status: ConsumerSetupStatus | null;
  values: Record<string, unknown>;
  onChange: (name: string, value: unknown) => void;
  loading: boolean;
  saving: "user" | "org" | null;
  error: string | null;
  disabled: boolean;
  onSaveUser: () => void;
  onSaveOrg: () => void;
}) {
  const valueByName = new Map((status?.values || []).map((item) => [item.name, item]));
  const missing = new Set(status?.missing_required || []);
  return (
    <SurfacePanel as="section" className="bg-runtime-bg/60 p-4">
      <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <h3 className="text-sm font-medium text-ink">Setup required</h3>
          <p className="mt-1 text-xs leading-relaxed text-ink-muted">
            Save the required values before launching this marketplace trial.
          </p>
        </div>
        <StatusBadge
          tone={loading ? "neutral" : status?.complete ? "emerald" : "amber"}
          className="shrink-0 rounded-md"
        >
          {loading
            ? "loading"
            : status?.complete
              ? "complete"
              : `${missing.size} missing`}
        </StatusBadge>
      </div>

      <div className="mt-4 grid gap-3">
        {fields.map((field) => {
          const current = valueByName.get(field.name);
          const configured = Boolean(current?.configured);
          const fieldMissing = missing.has(field.name);
          return (
            <label key={field.name} className="block">
              <span className="flex flex-wrap items-center gap-2 text-xs text-ink-muted">
                <span>{field.label || field.name}</span>
                {field.required && (
                  <StatusBadge tone="amber" className="rounded-md font-sans">
                    required
                  </StatusBadge>
                )}
                {configured && (
                  <StatusBadge tone="neutral" className="rounded-md font-mono text-[10px]">
                    {current?.source || "configured"}: {current?.value_redacted || "configured"}
                  </StatusBadge>
                )}
              </span>
              {field.description && (
                <span className="mt-1 block text-[11px] leading-relaxed text-ink-faint">
                  {field.description}
                </span>
              )}
              <ConsumerSetupFieldInput
                field={field}
                value={values[field.name]}
                onChange={(value) => onChange(field.name, value)}
                disabled={disabled || loading || saving !== null}
                attention={fieldMissing}
                className="mt-1"
              />
            </label>
          );
        })}
      </div>

      {error && (
        <InlineAlert tone="red" role="alert" className="mt-3">
          <span className="font-medium">Setup save failed: </span>
          {error}
        </InlineAlert>
      )}

      <div className="mt-4 flex flex-col gap-2 sm:flex-row sm:justify-end">
        <ToolbarButton
          type="button"
          onClick={onSaveUser}
          disabled={disabled || loading || saving !== null}
          variant="primary"
        >
          {saving === "user" ? "Saving..." : "Save for me"}
        </ToolbarButton>
        {status?.can_manage_org && (
          <ToolbarButton
            type="button"
            onClick={onSaveOrg}
            disabled={disabled || loading || saving !== null}
          >
            {saving === "org" ? "Saving..." : `Save for ${status.organization?.name || "org"}`}
          </ToolbarButton>
        )}
      </div>
    </SurfacePanel>
  );
}

function SkillSelect({
  skills,
  value,
  onChange,
}: {
  skills: AgentSkill[];
  value: string;
  onChange: (value: string) => void;
}) {
  if (skills.length === 0) {
    return (
      <InlineAlert tone="neutral">
        No declared skills. The trial will use the agent default.
      </InlineAlert>
    );
  }

  const selectedSkill = findSkill(skills, value);

  return (
    <FormField label="Tool" description={selectedSkill?.description}>
      <SelectInput
        value={value}
        onChange={(event) => onChange(event.target.value)}
      >
        {skills.map((skill) => (
          <option key={skill.name} value={skill.name}>
            {skill.name}
          </option>
        ))}
      </SelectInput>
    </FormField>
  );
}

function WorkspaceFileSelector({
  value,
  onChange,
  disabled,
  open,
}: {
  value: string[];
  onChange: (value: string[]) => void;
  disabled: boolean;
  open: boolean;
}) {
  const [files, setFiles] = useState<FileMeta[] | null>(null);
  const [query, setQuery] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const selected = useMemo(() => new Set(value), [value]);

  const refresh = useCallback(async () => {
    try {
      setFiles(await listFiles());
      setErr(null);
    } catch (ex) {
      setErr(messageFromError(ex));
    }
  }, []);

  useEffect(() => {
    if (!open || files !== null) return;
    refresh();
  }, [files, open, refresh]);

  const visibleFiles = useMemo(() => {
    const q = query.trim().toLowerCase();
    return [...(files || [])]
      .sort((left, right) => left.path.localeCompare(right.path))
      .filter((file) => {
        if (!q) return true;
        return (
          file.path.toLowerCase().includes(q) ||
          file.content_type.toLowerCase().includes(q)
        );
      });
  }, [files, query]);

  function toggle(path: string) {
    const next = selected.has(path)
      ? value.filter((item) => item !== path)
      : [...value, path];
    onChange(next);
  }

  return (
    <div>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <span className="text-xs text-ink-muted">Workspace files</span>
        <div className="flex items-center gap-2 text-xs">
          <span className="text-ink-faint">{value.length} selected</span>
          <ToolbarButton
            type="button"
            onClick={refresh}
            disabled={disabled}
            size="xs"
          >
            Refresh
          </ToolbarButton>
        </div>
      </div>
      <SurfacePanel as="div" className="mt-1 overflow-hidden bg-runtime-panel">
        <div className="border-b border-runtime-line-soft/60 p-2">
          <TextInput
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Search files"
            aria-label="Search workspace files"
            disabled={disabled}
          />
        </div>
        <div className="max-h-48 overflow-auto">
          {files === null ? (
            <LoadingState label="Loading files..." />
          ) : files.length === 0 ? (
            <EmptyState
              size="compact"
              title="No workspace files found"
              className="rounded-none border-0 bg-transparent px-3 py-3"
            />
          ) : visibleFiles.length === 0 ? (
            <EmptyState
              size="compact"
              title="No matching files"
              className="rounded-none border-0 bg-transparent px-3 py-3"
            />
          ) : (
            visibleFiles.map((file) => (
              <label
                key={file.path}
                className="flex cursor-pointer items-start gap-3 border-b border-runtime-line-soft px-3 py-2 last:border-b-0 hover:bg-runtime-raised/60"
              >
                <input
                  type="checkbox"
                  checked={selected.has(file.path)}
                  onChange={() => toggle(file.path)}
                  disabled={disabled}
                  className="mt-0.5 h-3.5 w-3.5 accent-ink-muted disabled:cursor-not-allowed"
                />
                <span className="min-w-0 flex-1">
                  <span className="block truncate font-mono text-xs text-ink-soft">
                    {file.path}
                  </span>
                  <span className="mt-0.5 block text-[11px] text-ink-faint">
                    {formatBytes(file.size)} | {file.content_type}
                  </span>
                </span>
              </label>
            ))
          )}
        </div>
      </SurfacePanel>
      {err && (
        <InlineAlert tone="red" className="mt-2">
          {err}
        </InlineAlert>
      )}
    </div>
  );
}

function paramsFromIntentInput(input?: MarketplaceTrialIntentInput) {
  if (input instanceof URLSearchParams) return new URLSearchParams(input);
  if (input instanceof URL) return new URLSearchParams(input.search);
  if (input && typeof input === "object") return new URLSearchParams(input.search);
  if (input == null) return new URLSearchParams(browserSearch());

  const trimmed = input.trim();
  if (!trimmed) return new URLSearchParams();
  if (trimmed.startsWith("?")) return new URLSearchParams(trimmed);
  if (trimmed.includes("://") || trimmed.startsWith("/")) {
    return new URL(trimmed, "http://marketplace.trials").searchParams;
  }
  if (trimmed.includes("?")) {
    const query = trimmed.slice(trimmed.indexOf("?") + 1).split("#", 1)[0];
    return new URLSearchParams(query);
  }
  return new URLSearchParams(trimmed);
}

function firstParam(params: URLSearchParams, keys: string[]) {
  for (const key of keys) {
    const value = params.get(key)?.trim();
    if (value) return value;
  }
  return "";
}

function isTrialIntentValue(value: string) {
  const normalized = value.trim().toLowerCase().replace(/[\s_]+/g, "-");
  return [
    "1",
    "true",
    "yes",
    "open",
    "new",
    "trial",
    "trials",
    "agent-trial",
    "run-trial",
    "launch-trial",
    "marketplace-trial",
  ].includes(normalized);
}

function preferredSkillNameFor(
  agent: AgentListing,
  skill?: AgentSkill | string | null,
) {
  const skills = agent.card?.skills || [];
  const skillName = typeof skill === "string" ? skill : skill?.name;
  if (skillName) return findSkill(skills, skillName)?.name || skillName;
  return skills[0]?.name || "";
}

function findSkill(skills: AgentSkill[], name: string) {
  return skills.find((skill) => sameName(skill.name, name)) || null;
}

function sameName(left: string, right: string) {
  return left.trim().toLowerCase() === right.trim().toLowerCase();
}

function initialSetupValues(status: ConsumerSetupStatus): Record<string, unknown> {
  const configured = new Set(
    status.values.filter((value) => value.configured).map((value) => value.name),
  );
  const out: Record<string, unknown> = {};
  for (const field of status.declaration.fields) {
    if (!configured.has(field.name)) out[field.name] = "";
  }
  return out;
}

function payloadSetupValues(
  fields: ConsumerSetupField[],
  values: Record<string, unknown>,
): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const field of fields) {
    const value = values[field.name];
    if (value === undefined || value === null || value === "") continue;
    out[field.name] = value;
  }
  return out;
}

function marketplaceLaunchBlockReason(agent: AgentListing) {
  if (!agent.public) return "This agent is not public in the marketplace.";
  if (agent.status !== "running") return `This agent is ${agent.status}.`;
  return null;
}

function trialTitle(agentName: string, skillName: string) {
  return `Marketplace trial: ${agentName}${skillName ? ` / ${skillName}` : ""}`.slice(
    0,
    180,
  );
}

function parseWorkspacePaths(paths: string) {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const path of paths.split(/[\n,]/)) {
    const clean = path.trim().replace(/^\/+/, "");
    if (!clean || seen.has(clean)) continue;
    seen.add(clean);
    out.push(clean);
  }
  return out;
}

function validateOptionalJson(value: string) {
  const trimmed = value.trim();
  if (!trimmed) return null;
  try {
    JSON.parse(trimmed);
    return null;
  } catch (ex) {
    return ex instanceof Error ? ex.message : "Invalid JSON.";
  }
}

function intentSignature(intent: MarketplaceTrialQueryIntent) {
  return [
    intent.open ? "1" : "0",
    intent.agent,
    intent.skill,
    intent.goal,
    intent.criteria,
    intent.files.join(","),
    intent.argsJson,
  ].join("\u0000");
}

function messageFromError(ex: unknown) {
  return ex instanceof Error ? ex.message : String(ex);
}

function browserSearch() {
  return typeof window === "undefined" ? "" : window.location.search;
}

function formatBytes(n: number) {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}
