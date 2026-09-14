import { publicAgentUrl } from "../lib/publicAgentUrl";
import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import {
  getAgentDeployment,
  claimGuestStudioRun,
  decideStudioUpgradeProposal,
  getStudioAutopilot,
  getStudioUpgradeProposal,
  updateAgentVisibility,
  getStudioRun,
  resolveStudioRun,
  listStudioUpgradeProposals,
  runStudioAutopilotNow,
  runStudioIdeaFactory,
  startStudioRun,
  streamStudioRun,
  updateStudioAutopilot,
  type AgentDeployment,
  type StudioAutopilotPolicy,
  type StudioReviewDepth,
  type StudioResolution,
  type StudioIdeaFactoryResult,
  type StudioReuseAction,
  type StudioReuseCandidate,
  type StudioRun,
  type StudioRunBrief,
  type StudioRunEvent,
  type StudioUpgradeProposal,
} from "../api";
import { AgentDeploymentTimeline } from "./AgentDeploymentTimeline";
import {
  CopyButton,
  FormField,
  InlineAlert,
  SegmentedButton,
  SegmentedControl,
  SurfacePanel,
  TextArea,
  TextInput,
  ToggleField,
  ToolbarButton,
  type StatusBadgeTone,
} from "./DashboardChrome";
import { StatusBadge } from "./StatusPillAdapters";
import {
  captureStudioPrefillFromHash,
  clearStoredStudioClaim,
  clearStoredStudioPrefill,
  readStoredStudioClaim,
  readStoredStudioPrefill,
} from "../studioPrefill";

// ---------------------------------------------------------------------------
// Agent Studio: describe an agent once; a crew builds, reviews, improves, and
// ships it. This screen owns the brief (below) and hands off to a live run
// view. The run itself is driven by the deployed agent-studio coordinator.
// ---------------------------------------------------------------------------

export function AgentStudio() {
  const { runId } = useParams<{ runId?: string }>();
  const [searchParams] = useSearchParams();
  const proposalId = searchParams.get("proposal");
  const requestedDecision = searchParams.get("decision") === "reject" ? "reject" : "accept";
  const claimToken = typeof window === "undefined"
    ? null
    : new URLSearchParams(window.location.hash.replace(/^#/, "")).get("claim") ?? readStoredStudioClaim();
  if (claimToken) return <StudioClaim token={claimToken} />;
  if (proposalId) {
    return <StudioProposalDecision proposalId={proposalId} requestedDecision={requestedDecision} />;
  }
  return runId ? <StudioRunView runId={runId} /> : <StudioBrief />;
}

function StudioProposalDecision({
  proposalId,
  requestedDecision,
}: {
  proposalId: string;
  requestedDecision: "accept" | "reject";
}) {
  const navigate = useNavigate();
  const [proposal, setProposal] = useState<StudioUpgradeProposal | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    void getStudioUpgradeProposal(proposalId)
      .then((value) => { if (!cancelled) setProposal(value); })
      .catch((reason) => {
        if (!cancelled) setError(reason instanceof Error ? reason.message : "Could not load this idea.");
      });
    return () => { cancelled = true; };
  }, [proposalId]);

  useEffect(() => {
    if (!proposal || !["accepted", "applying"].includes(proposal.status)) return;
    const timer = window.setInterval(() => {
      void getStudioUpgradeProposal(proposalId).then(setProposal).catch(() => undefined);
    }, 2500);
    return () => window.clearInterval(timer);
  }, [proposal, proposalId]);

  const decide = async (decision: "accept" | "reject") => {
    setBusy(true);
    setError(null);
    try {
      setProposal(await decideStudioUpgradeProposal(proposalId, decision));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not save your decision.");
    } finally {
      setBusy(false);
    }
  };

  const status = proposal?.status ?? "loading";
  const terminal = ["applied", "failed", "rejected", "expired"].includes(status);
  return (
    <div className="mx-auto max-w-2xl space-y-4 p-1">
      <header className="space-y-2">
        <div className="text-[10px] uppercase tracking-[0.18em] text-brand-volt">
          Agent Studio · approval
        </div>
        <h1 className="text-2xl font-semibold text-ink">
          {proposal ? `Upgrade ${proposal.agent_name}?` : "Loading upgrade idea…"}
        </h1>
        <p className="text-sm leading-6 text-ink-dim">
          Review the exact idea before Agent Studio receives permission to change code.
        </p>
      </header>
      {error && <InlineAlert tone="red">{error}</InlineAlert>}
      {proposal && (
        <SurfacePanel className="space-y-5 p-5">
          <div className="flex flex-wrap items-center gap-2">
            <StatusBadge
              tone={proposal.status === "applied" ? "emerald" : proposal.status === "failed" ? "red" : "amber"}
            >
              {proposal.status}
            </StatusBadge>
            <span className="rounded-full border border-line px-2 py-1 font-mono text-[10px] uppercase text-ink-muted">
              {proposal.severity} · {proposal.category}
            </span>
          </div>
          <div>
            <h2 className="text-lg font-semibold text-ink">{proposal.title}</h2>
            <p className="mt-2 text-sm leading-6 text-ink-dim">{proposal.idea}</p>
            <p className="mt-3 text-xs leading-5 text-ink-muted">{proposal.rationale}</p>
          </div>
          {proposal.source_head_sha && (
            <div className="rounded-lg border border-line bg-runtime-bg/50 p-3 font-mono text-[11px] text-ink-muted">
              Reviewed source {proposal.source_head_sha.slice(0, 12)}. If the source changed, Studio will stop and request a fresh review.
            </div>
          )}
          {proposal.error && <InlineAlert tone="red">{proposal.error}</InlineAlert>}
          {status === "pending" && (
            <div className="flex flex-wrap gap-2 border-t border-line pt-4">
              <ToolbarButton
                variant={requestedDecision === "accept" ? "primary" : "secondary"}
                size="md"
                disabled={busy}
                onClick={() => void decide("accept")}
              >
                Accept &amp; upgrade
              </ToolbarButton>
              <ToolbarButton
                variant={requestedDecision === "reject" ? "danger" : "secondary"}
                size="md"
                disabled={busy}
                onClick={() => void decide("reject")}
              >
                Reject idea
              </ToolbarButton>
            </div>
          )}
          {["accepted", "applying"].includes(status) && (
            <InlineAlert tone="amber">
              Agent Studio is patching, testing, and deploying this upgrade. You can close this page; the decision is durable.
            </InlineAlert>
          )}
          {terminal && (
            <div className="flex flex-wrap gap-2 border-t border-line pt-4">
              <ToolbarButton variant="primary" size="md" onClick={() => navigate(`/my-agents/${encodeURIComponent(proposal.agent_name)}`)}>
                Open agent
              </ToolbarButton>
              <ToolbarButton size="md" onClick={() => navigate("/studio", { replace: true })}>
                Back to Studio
              </ToolbarButton>
            </div>
          )}
        </SurfacePanel>
      )}
    </div>
  );
}

function StudioClaim({ token }: { token: string }) {
  const navigate = useNavigate();
  const [state, setState] = useState<"claiming" | "claimed" | "publishing" | "published" | "failed">("claiming");
  const [message, setMessage] = useState("Claiming your finished agent…");
  const [agentName, setAgentName] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    void claimGuestStudioRun(token)
      .then((result) => {
        if (cancelled) return;
        clearStoredStudioClaim();
        window.history.replaceState(null, "", window.location.pathname);
        setAgentName(result.agent_name);
        if (result.public) {
          setState("published");
          setMessage("Your agent is yours and already listed in the public registry.");
        } else {
          setState("claimed");
          setMessage("Your agent is yours. Choose whether to list it publicly or keep it unlisted.");
        }
      })
      .catch((error) => {
        if (cancelled) return;
        setState("failed");
        setMessage(error instanceof Error ? error.message : "Could not claim this agent.");
      });
    return () => { cancelled = true; };
  }, [navigate, token]);

  const publish = async () => {
    if (!agentName || state === "publishing") return;
    setState("publishing");
    setMessage("Publishing the Agent Card and tools to the registry…");
    try {
      await updateAgentVisibility(agentName, true);
      setState("published");
      setMessage("Your agent is live in the public Agent Registry.");
    } catch (error) {
      setState("claimed");
      setMessage(error instanceof Error ? error.message : "Could not publish this agent.");
    }
  };

  const heading = state === "claiming"
    ? "Adding the agent to your account"
    : state === "claimed" || state === "publishing"
      ? "Your agent is yours"
      : state === "published"
        ? "Published to Agent Registry"
        : "Claim needs attention";

  return (
    <div className="mx-auto max-w-xl p-4">
      <SurfacePanel className="space-y-4 p-6 text-center">
        <div className="font-mono text-[10px] uppercase tracking-[0.16em] text-brand-volt">Agent claim</div>
        <h1 className="text-xl font-semibold text-ink">{heading}</h1>
        <p className="text-sm leading-6 text-ink-dim">{message}</p>
        {state === "claimed" && agentName && (
          <div className="grid gap-2 sm:grid-cols-2">
            <button type="button" onClick={() => void publish()} className="inline-flex min-h-11 items-center justify-center rounded-md bg-brand-volt px-4 text-sm font-semibold text-runtime-bg">
              Publish to Agent Registry
            </button>
            <button type="button" onClick={() => navigate(`/my-agents/${encodeURIComponent(agentName)}`, { replace: true })} className="inline-flex min-h-11 items-center justify-center rounded-md border border-line px-4 text-sm text-ink">
              Keep unlisted
            </button>
          </div>
        )}
        {state === "publishing" && (
          <button type="button" disabled className="inline-flex min-h-11 items-center justify-center rounded-md bg-brand-volt px-4 text-sm font-semibold text-runtime-bg opacity-70">
            Publishing…
          </button>
        )}
        {state === "published" && agentName && (
          <div className="grid gap-2 sm:grid-cols-2">
            <a href={publicAgentUrl(agentName)} className="inline-flex min-h-11 items-center justify-center rounded-md bg-brand-volt px-4 text-sm font-semibold text-runtime-bg">
              Open agent URL ↗
            </a>
            <button type="button" onClick={() => navigate(`/my-agents/${encodeURIComponent(agentName)}`, { replace: true })} className="inline-flex min-h-11 items-center justify-center rounded-md border border-line px-4 text-sm text-ink">
              Open my agent
            </button>
          </div>
        )}
        {state === "failed" && <a href="/studio" className="inline-flex min-h-10 items-center justify-center rounded-md border border-line px-4 text-sm text-ink">Return to Agent Studio</a>}
      </SurfacePanel>
    </div>
  );
}

// ---------------------------------------------------------------------------
// The brief — one expressive goal, then progressive controls. No 7-step wizard.
// ---------------------------------------------------------------------------

const REVIEW_OPTIONS: { id: StudioReviewDepth; label: string; hint: string; loops: number }[] = [
  { id: "light", label: "Light", hint: "1 pass, no auto-repair", loops: 1 },
  { id: "standard", label: "Standard", hint: "review + fix once", loops: 2 },
  { id: "strict", label: "Strict", hint: "review + fix, up to 3 loops", loops: 3 },
];

const BUDGET_OPTIONS = [
  { cents: 100, label: "$1" },
  { cents: 500, label: "$5" },
  { cents: 3000, label: "$30" },
];

const RECIPE_OPTIONS = [
  ["auto", "Auto-pick"],
  ["csv_tool", "CSV tool"],
  ["document_generator", "Document generator"],
  ["email_assistant", "Email assistant"],
  ["scheduled_monitor", "Scheduled monitor"],
  ["calculator", "Calculator"],
  ["approval_workflow", "Approval workflow"],
  ["dashboard", "Dashboard"],
  ["custom", "Custom"],
] as const;

const REUSE_ACTIONS: { id: Exclude<StudioReuseAction, "build_new">; label: string }[] = [
  { id: "use_existing", label: "Use existing" },
  { id: "compose", label: "Compose" },
  { id: "fork", label: "Fork source" },
  { id: "edit_existing", label: "Edit in place" },
];

// Cheap ghostwriter: surface likely integrations from the goal text so the user
// confirms rather than hunts. Not exhaustive — a starting point they edit.
const INTEGRATION_HINTS: { match: RegExp; name: string }[] = [
  { match: /\b(e-?mails?|inbox|gmail)\b/i, name: "Gmail" },
  { match: /\b(stripe|invoice|charge|refund|billing|payment)\b/i, name: "Stripe" },
  { match: /\bslack\b/i, name: "Slack" },
  { match: /\b(github|pull request|\bpr\b|repo)\b/i, name: "GitHub" },
  { match: /\b(notion|wiki|docs?)\b/i, name: "Notion" },
  { match: /\b(calendar|schedule|meeting)\b/i, name: "Google Calendar" },
];

function StudioIdeaFactory() {
  const navigate = useNavigate();
  const [theme, setTheme] = useState("useful one-page tools for small businesses");
  const [result, setResult] = useState<StudioIdeaFactoryResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const run = async (buildTopThree: boolean) => {
    if (theme.trim().length < 3 || busy) return;
    setBusy(true);
    setError(null);
    try {
      const idempotencyKey = typeof crypto !== "undefined" && "randomUUID" in crypto
        ? crypto.randomUUID()
        : `idea-factory-${Date.now()}-${Math.random().toString(16).slice(2)}`;
      const next = await runStudioIdeaFactory(theme.trim(), buildTopThree, idempotencyKey);
      setResult(next);
      if (buildTopThree && next.runs[0]) navigate(`/studio/runs/${next.runs[0].run_id}`);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not generate startup ideas.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <SurfacePanel as="section" className="space-y-4 p-4">
      <div>
        <div className="text-[10px] uppercase tracking-[0.16em] text-brand-volt">100-idea factory</div>
        <h2 className="mt-1 text-base font-semibold text-ink">Find the strongest tiny agent business</h2>
        <p className="mt-1 text-xs leading-5 text-ink-dim">
          Score 100 one-page startup ideas on usefulness, recurrence, viability, buildability, and platform fit. Launch the best three concurrently inside one $30 total budget.
        </p>
      </div>
      <div className="flex flex-col gap-2 sm:flex-row">
        <TextInput value={theme} onChange={(event) => setTheme(event.target.value)} placeholder="Tools for local service businesses" />
        <ToolbarButton disabled={busy || theme.trim().length < 3} onClick={() => void run(false)}>
          {busy ? "Scoring…" : "Generate 100"}
        </ToolbarButton>
        <ToolbarButton className="bg-brand-volt text-runtime-bg" disabled={busy || theme.trim().length < 3} onClick={() => void run(true)}>
          Build best 3
        </ToolbarButton>
      </div>
      {error && <InlineAlert tone="red">{error}</InlineAlert>}
      {result && (
        <div className="grid gap-2 sm:grid-cols-3">
          {result.top_three.map((idea) => (
            <div key={idea.idea_id} className="rounded-lg border border-line bg-runtime-bg/40 p-3">
              <div className="font-mono text-[10px] text-brand-volt">#{idea.rank} · {idea.scores.total.toFixed(2)}</div>
              <div className="mt-1 text-xs font-semibold text-ink">{idea.title}</div>
              <div className="mt-2 flex flex-wrap gap-1">
                {idea.mcp_tools.map((tool) => <span key={tool} className="rounded border border-line px-1 py-0.5 font-mono text-[9px] text-ink-muted">{tool}</span>)}
              </div>
            </div>
          ))}
        </div>
      )}
    </SurfacePanel>
  );
}

function StudioAutopilot() {
  const browserTimezone = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  const [policy, setPolicy] = useState<StudioAutopilotPolicy | null>(null);
  const [proposals, setProposals] = useState<StudioUpgradeProposal[]>([]);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    void Promise.all([getStudioAutopilot(), listStudioUpgradeProposals()])
      .then(([nextPolicy, nextProposals]) => {
        if (cancelled) return;
        setPolicy(nextPolicy);
        setProposals(nextProposals);
      })
      .catch((reason) => {
        if (!cancelled) setError(reason instanceof Error ? reason.message : "Could not load daily reviews.");
      });
    return () => { cancelled = true; };
  }, []);

  useEffect(() => {
    if (!policy || !["queued", "running"].includes(policy.last_run_status ?? "")) return;
    const timer = window.setInterval(() => {
      void Promise.all([getStudioAutopilot(), listStudioUpgradeProposals()])
        .then(([nextPolicy, nextProposals]) => {
          setPolicy(nextPolicy);
          setProposals(nextProposals);
        })
        .catch(() => undefined);
    }, 3000);
    return () => window.clearInterval(timer);
  }, [policy]);

  const save = async (enabled: boolean, dailyHour = policy?.daily_hour ?? 9) => {
    setSaving(true);
    setError(null);
    try {
      const firstEnable = enabled && !policy?.enabled && policy?.last_run_status == null;
      setPolicy(await updateStudioAutopilot({
        enabled,
        timezone: firstEnable ? browserTimezone : policy?.timezone || browserTimezone,
        daily_hour: dailyHour,
      }));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not update daily reviews.");
    } finally {
      setSaving(false);
    }
  };

  const runNow = async () => {
    setSaving(true);
    setError(null);
    try {
      setPolicy(await runStudioAutopilotNow());
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not start the review.");
    } finally {
      setSaving(false);
    }
  };

  const pending = proposals.filter((proposal) => proposal.status === "pending");
  return (
    <SurfacePanel as="section" className="space-y-4 p-4">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <div className="text-[10px] uppercase tracking-[0.16em] text-brand-volt">
            Daily review
          </div>
          <h2 className="mt-1 text-base font-semibold text-ink">Keep your agents improving</h2>
          <p className="mt-1 max-w-xl text-xs leading-5 text-ink-dim">
            Studio reads each managed agent and its source once a day, emails concrete gaps and ideas, and waits for your approval before changing code.
          </p>
        </div>
        {policy && (
          <ToggleField
            label={policy.enabled ? "Daily review on" : "Daily review off"}
            description="Code changes always require approval."
            checked={policy.enabled}
            disabled={saving}
            onCheckedChange={(enabled) => void save(enabled)}
          />
        )}
      </div>
      {error && <InlineAlert tone="red">{error}</InlineAlert>}
      {policy?.enabled && (
        <div className="flex flex-wrap items-end gap-3 border-t border-line pt-4">
          <FormField label="Review time" description={policy.timezone}>
            <select
              aria-label="Daily review hour"
              value={policy.daily_hour}
              disabled={saving}
              onChange={(event) => void save(true, Number(event.target.value))}
              className="h-9 rounded-lg border border-line bg-runtime-bg px-3 text-sm text-ink outline-none focus:border-brand-volt"
            >
              {Array.from({ length: 24 }, (_, hour) => (
                <option key={hour} value={hour}>{String(hour).padStart(2, "0")}:00</option>
              ))}
            </select>
          </FormField>
          <ToolbarButton size="md" disabled={saving || ["queued", "running"].includes(policy.last_run_status ?? "")} onClick={() => void runNow()}>
            {["queued", "running"].includes(policy.last_run_status ?? "") ? "Review running…" : "Run now"}
          </ToolbarButton>
          {policy.next_run_at && (
            <span className="pb-2 text-[11px] text-ink-muted">
              Next {new Date(policy.next_run_at).toLocaleString()}
            </span>
          )}
        </div>
      )}
      {pending.length > 0 && (
        <div className="space-y-2 border-t border-line pt-4">
          <div className="text-[10px] uppercase tracking-[0.14em] text-ink-faint">
            Awaiting your decision · {pending.length}
          </div>
          {pending.slice(0, 3).map((proposal) => (
            <a
              key={proposal.proposal_id}
              href={`/studio?proposal=${encodeURIComponent(proposal.proposal_id)}&decision=accept`}
              className="flex items-center justify-between gap-3 rounded-lg border border-line bg-runtime-bg/40 px-3 py-2 transition hover:border-brand-volt/50"
            >
              <span className="min-w-0">
                <span className="block truncate text-xs font-medium text-ink">{proposal.title}</span>
                <span className="block text-[11px] text-ink-muted">{proposal.agent_name}</span>
              </span>
              <span className="shrink-0 text-xs text-brand-volt">Review →</span>
            </a>
          ))}
        </div>
      )}
    </SurfacePanel>
  );
}

function slugify(text: string): string {
  return text
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .split("-")
    .slice(0, 4)
    .join("-");
}

function StudioBrief() {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const [fragmentPrefill] = useState(() => {
    return captureStudioPrefillFromHash() ?? readStoredStudioPrefill();
  });
  const prefilledGoal = fragmentPrefill?.goal
    ?? (searchParams.get("goal") ?? "").trim().slice(0, 800);
  const requestedName = (searchParams.get("name") ?? "").trim().toLowerCase();
  const prefilledName = fragmentPrefill?.name
    ?? (/^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$/.test(requestedName)
      ? requestedName
      : "");
  const fromIdeaFunnel = searchParams.get("source") === "idea-funnel" && Boolean(prefilledGoal);
  const [goal, setGoal] = useState(prefilledGoal);
  const [name, setName] = useState(prefilledName);
  const [nameTouched, setNameTouched] = useState(Boolean(prefilledName));
  const [inputs, setInputs] = useState<string[]>([]);
  const [integrations, setIntegrations] = useState<string[]>([]);
  const [frontend, setFrontend] = useState(true);
  const [budgetCents, setBudgetCents] = useState(500);
  const [isPublic, setIsPublic] = useState(false);
  const [recipe, setRecipe] = useState<StudioRunBrief["recipe"]>("auto");
  const [accountTrialCalls, setAccountTrialCalls] = useState(0);
  const [review, setReview] = useState<StudioReviewDepth>("standard");
  const [submitting, setSubmitting] = useState(false);
  const [resolution, setResolution] = useState<StudioResolution | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (fragmentPrefill) clearStoredStudioPrefill();
  }, [fragmentPrefill]);

  // Ghostwriter suggestions derived from the goal.
  const suggestedName = useMemo(() => slugify(goal), [goal]);
  const suggestedIntegrations = useMemo(() => {
    const hits = INTEGRATION_HINTS.filter((h) => h.match.test(goal)).map((h) => h.name);
    return hits.filter((h) => !integrations.includes(h));
  }, [goal, integrations]);

  const effectiveName = nameTouched ? name : suggestedName;
  const nameValid = /^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$/.test(effectiveName);
  const canBuild = goal.trim().length >= 12 && nameValid && !submitting;

  useEffect(() => {
    setResolution(null);
  }, [goal, effectiveName, inputs, integrations, frontend, budgetCents, isPublic, recipe, accountTrialCalls, review]);

  const resolve = useCallback(async () => {
    if (!canBuild) return;
    setSubmitting(true);
    setError(null);
    const brief: StudioRunBrief = {
      name: effectiveName,
      goal: goal.trim(),
      inputs,
      integrations,
      frontend,
      budget_cents: budgetCents,
      public: isPublic,
      recipe,
      account_trial_calls: isPublic ? accountTrialCalls : 0,
      review,
    };
    try {
      const next = await resolveStudioRun(brief);
      setResolution(next);
      setSubmitting(false);
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Could not resolve reusable agents. Please try again.",
      );
      setSubmitting(false);
    }
  }, [
    canBuild, effectiveName, goal, inputs, integrations, frontend, budgetCents,
    isPublic, recipe, accountTrialCalls, review,
  ]);

  const execute = useCallback(async (
    action: StudioReuseAction,
    candidate?: StudioReuseCandidate,
  ) => {
    if (!resolution || submitting) return;
    if (action === "edit_existing" && !window.confirm(
      `Edit ${candidate?.name ?? "this agent"} in place at the pinned source revision?`,
    )) return;
    setSubmitting(true);
    setError(null);
    const idempotencyKey = typeof crypto !== "undefined" && "randomUUID" in crypto
      ? crypto.randomUUID()
      : `studio-${Date.now()}-${Math.random().toString(16).slice(2)}`;
    const brief: StudioRunBrief = {
      name: effectiveName,
      goal: goal.trim(),
      inputs,
      integrations,
      frontend,
      budget_cents: budgetCents,
      public: isPublic,
      recipe,
      account_trial_calls: isPublic ? accountTrialCalls : 0,
      review,
      plan_id: resolution.plan_id,
      action,
      candidate_agent_id: candidate?.agent_id ?? null,
      expected_version: candidate?.version ?? null,
      expected_card_hash: candidate?.card_hash ?? null,
      expected_source_sha: candidate?.source_sha ?? null,
      idempotency_key: idempotencyKey,
      confirmed_edit: action === "edit_existing",
    };
    try {
      const run = await startStudioRun(brief);
      navigate(`/studio/runs/${run.run_id}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not execute this Studio plan.");
      setSubmitting(false);
    }
  }, [
    resolution, submitting, effectiveName, goal, inputs, integrations, frontend,
    budgetCents, isPublic, recipe, accountTrialCalls, review, navigate,
  ]);

  return (
    <div className="mx-auto max-w-3xl space-y-5 p-1">
      <header className="space-y-2">
        <div className="text-[10px] uppercase tracking-[0.18em] text-ink-faint">
          Agent Studio
        </div>
        <h1 className="text-balance text-2xl font-semibold text-ink">
          Describe your agent. A crew builds, reviews, repairs, and ships it.
        </h1>
        <p className="max-w-2xl text-sm leading-relaxed text-ink-dim">
          State the goal in plain language. The builder scaffolds it, the reviewer
          hunts for flaws, the editor fixes them, and it deploys privately with a
          live URL, a connector, and a signed proof.
        </p>
      </header>

      {error && (
        <InlineAlert tone="red" role="alert" className="text-xs">
          {error}
        </InlineAlert>
      )}

      {fromIdeaFunnel && !error && (
        <InlineAlert tone="emerald" className="text-xs">
          Your funnel goal and agent name are loaded. Review the remaining controls, then start the build.
        </InlineAlert>
      )}

      <StudioIdeaFactory />
      <StudioAutopilot />

      <SurfacePanel as="section" className="space-y-5 p-4">
        <div data-onboarding-target="studio-brief">
          <FormField
            label="Goal"
            description="What should this agent do? One or two sentences is plenty."
          >
            <TextArea
              value={goal}
              onChange={(e) => setGoal(e.target.value)}
              rows={3}
              placeholder="A support triage agent that reads incoming emails, classifies urgency, drafts a reply, and escalates billing disputes to a human."
            />
          </FormField>
        </div>

        <div className="grid gap-4 sm:grid-cols-2">
          <FormField
            label="Name"
            description={
              nameValid || !effectiveName
                ? "Lowercase, hyphenated. Used in the agent's URL."
                : "Use lowercase letters, numbers, and hyphens (3–40 chars)."
            }
          >
            <TextInput
              value={effectiveName}
              invalid={!!effectiveName && !nameValid}
              mono
              onChange={(e) => {
                setNameTouched(true);
                setName(e.target.value);
              }}
              placeholder={suggestedName || "triage-desk"}
            />
          </FormField>

          <FormField label="Frontend">
            <ToggleField
              label="Ship a chat card"
              description="Give the agent a simple UI people can talk to."
              checked={frontend}
              onCheckedChange={setFrontend}
            />
          </FormField>
        </div>

        <ChipEditor
          label="Inputs"
          hint="What each run receives. Press Enter to add."
          placeholder="email_thread"
          values={inputs}
          onChange={setInputs}
        />

        <div className="grid gap-4 sm:grid-cols-2">
          <FormField label="Mini-startup recipe" description="Auto-pick uses the goal and integrations.">
            <select
              value={recipe}
              onChange={(event) => setRecipe(event.target.value as StudioRunBrief["recipe"])}
              className="h-10 w-full rounded-lg border border-line bg-runtime-bg px-3 text-sm text-ink outline-none focus:border-brand-volt"
            >
              {RECIPE_OPTIONS.map(([value, label]) => (
                <option key={value} value={value}>{label}</option>
              ))}
            </select>
          </FormField>
          <FormField label="Account trial" description="Platform-funded skill calls, then the caller brings an LLM key.">
            <SegmentedControl aria-label="Account trial calls" className="w-full">
              {[0, 3, 10].map((calls) => (
                <SegmentedButton
                  key={calls}
                  selected={accountTrialCalls === calls}
                  onClick={() => setAccountTrialCalls(calls)}
                  disabled={!isPublic}
                  className="flex-1"
                >
                  {calls === 0 ? "Off" : `${calls} calls`}
                </SegmentedButton>
              ))}
            </SegmentedControl>
          </FormField>
        </div>

        <ChipEditor
          label="Integrations"
          hint="Connectors the agent may use. Grants are requested at deploy."
          placeholder="Gmail"
          values={integrations}
          onChange={setIntegrations}
          suggestions={suggestedIntegrations}
        />

        <div className="grid gap-4 sm:grid-cols-3">
          <FormField label="Budget" description="Hard ceiling for this build.">
            <SegmentedControl aria-label="Build budget" className="w-full">
              {BUDGET_OPTIONS.map((opt) => (
                <SegmentedButton
                  key={opt.cents}
                  selected={budgetCents === opt.cents}
                  onClick={() => setBudgetCents(opt.cents)}
                  className="flex-1"
                >
                  {opt.label}
                </SegmentedButton>
              ))}
            </SegmentedControl>
          </FormField>

          <FormField label="Privacy" description="Publishing stays opt-in.">
            <SegmentedControl aria-label="Privacy" className="w-full">
              <SegmentedButton selected={!isPublic} onClick={() => setIsPublic(false)} className="flex-1">
                Private
              </SegmentedButton>
              <SegmentedButton selected={isPublic} onClick={() => setIsPublic(true)} className="flex-1">
                Public
              </SegmentedButton>
            </SegmentedControl>
          </FormField>

          <FormField
            label="Review"
            description={REVIEW_OPTIONS.find((r) => r.id === review)?.hint}
          >
            <SegmentedControl aria-label="Review depth" className="w-full">
              {REVIEW_OPTIONS.map((opt) => (
                <SegmentedButton
                  key={opt.id}
                  selected={review === opt.id}
                  onClick={() => setReview(opt.id)}
                  className="flex-1"
                >
                  {opt.label}
                </SegmentedButton>
              ))}
            </SegmentedControl>
          </FormField>
        </div>
      </SurfacePanel>

      {resolution && (
        <SurfacePanel as="section" className="space-y-4 p-4">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <div className="text-[10px] uppercase tracking-[0.18em] text-signal-protocol">
                Reuse before build
              </div>
              <h2 className="mt-1 text-base font-semibold text-ink">
                {resolution.candidates.length
                  ? `${resolution.candidates.length} existing agent${resolution.candidates.length === 1 ? "" : "s"} may fit`
                  : "No close existing match found"}
              </h2>
              <p className="mt-1 text-xs leading-5 text-ink-muted">
                Actions are authorized separately. Public listing never grants source access.
              </p>
            </div>
            <ToolbarButton
              data-onboarding-target="studio-build"
              onClick={() => void execute("build_new")}
              disabled={submitting}
              className="bg-signal-protocol text-runtime-bg"
            >
              Build new anyway
            </ToolbarButton>
          </div>

          <div className="space-y-3">
            {resolution.candidates.map((candidate) => (
              <article key={candidate.candidate_id} className="rounded-xl border border-runtime-line-soft/70 bg-runtime-soft p-3">
                <div className="flex flex-wrap items-start justify-between gap-2">
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="font-mono text-sm font-semibold text-ink">{candidate.name}</span>
                      <StatusBadge tone={candidate.healthy ? "emerald" : "amber"} className="text-[10px]">
                        {candidate.status}
                      </StatusBadge>
                      <span className="font-mono text-[10px] text-ink-faint">v{candidate.version}</span>
                    </div>
                    <p className="mt-1 text-xs leading-5 text-ink-dim">{candidate.description}</p>
                    <div className="mt-2 flex flex-wrap gap-1.5">
                      {candidate.skills.slice(0, 5).map((skill) => (
                        <span key={skill.name} className="rounded border border-runtime-line-soft px-1.5 py-0.5 font-mono text-[10px] text-ink-muted">
                          {skill.name}
                        </span>
                      ))}
                      {!candidate.setup.complete && (
                        <span className="rounded border border-signal-authority/40 px-1.5 py-0.5 text-[10px] text-signal-authority">
                          setup required
                        </span>
                      )}
                    </div>
                  </div>
                  <div className="font-mono text-[10px] text-ink-faint">
                    match {candidate.score.toFixed(1)}
                  </div>
                </div>
                <div className="mt-3 flex flex-wrap gap-2">
                  {REUSE_ACTIONS.map((action) => {
                    const decision = candidate.actions[action.id];
                    return (
                      <ToolbarButton
                        key={action.id}
                        onClick={() => void execute(action.id, candidate)}
                        disabled={submitting || !decision.allowed}
                        title={decision.reason}
                      >
                        {action.label}
                      </ToolbarButton>
                    );
                  })}
                </div>
              </article>
            ))}
          </div>
        </SurfacePanel>
      )}

      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-xs text-ink-muted">
          {isPublic ? "Deploys, then awaits your review before publishing." : "Deploys privately to your account."}
          {" "}Up to {REVIEW_OPTIONS.find((r) => r.id === review)?.loops} review loop
          {REVIEW_OPTIONS.find((r) => r.id === review)?.loops === 1 ? "" : "s"}.
        </p>
        <ToolbarButton
          data-onboarding-target="studio-start"
          onClick={() => void resolve()}
          disabled={!canBuild}
          className="bg-signal-protocol px-5 py-2.5 font-semibold text-runtime-bg hover:bg-signal-protocol-strong disabled:cursor-not-allowed disabled:opacity-40"
        >
          {submitting
            ? "Checking…"
            : resolution
              ? "↻ Refresh reusable agents"
              : "◇ Find reusable agents"}
        </ToolbarButton>
      </div>
    </div>
  );
}

function ChipEditor({
  label,
  hint,
  placeholder,
  values,
  onChange,
  suggestions = [],
}: {
  label: string;
  hint: string;
  placeholder: string;
  values: string[];
  onChange: (next: string[]) => void;
  suggestions?: string[];
}) {
  const [draft, setDraft] = useState("");
  const add = (raw: string) => {
    const v = raw.trim();
    if (v && !values.includes(v)) onChange([...values, v]);
    setDraft("");
  };
  return (
    <FormField label={label} description={hint}>
      <div className="flex flex-wrap items-center gap-2 rounded-md border border-runtime-line-soft/60 bg-runtime-bg/40 p-2">
        {values.map((v) => (
          <span
            key={v}
            className="inline-flex items-center gap-1 rounded-md border border-runtime-line bg-runtime-raised px-2 py-1 font-mono text-xs text-ink-soft"
          >
            {v}
            <button
              type="button"
              aria-label={`Remove ${v}`}
              onClick={() => onChange(values.filter((x) => x !== v))}
              className="text-ink-faint hover:text-signal-danger focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-signal-danger"
            >
              ×
            </button>
          </span>
        ))}
        <input
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              add(draft);
            } else if (e.key === "Backspace" && !draft && values.length) {
              onChange(values.slice(0, -1));
            }
          }}
          placeholder={values.length ? "" : placeholder}
          className="min-w-24 flex-1 bg-transparent font-mono text-xs text-ink-soft outline-none placeholder:text-ink-faint"
        />
      </div>
      {suggestions.length > 0 && (
        <div className="mt-1.5 flex flex-wrap items-center gap-1.5 text-xs text-ink-muted">
          <span className="text-signal-protocol">✦ suggested:</span>
          {suggestions.map((s) => (
            <button
              key={s}
              type="button"
              onClick={() => add(s)}
              className="rounded-md border border-runtime-line-soft/60 px-2 py-0.5 font-mono text-ink-soft hover:border-signal-protocol hover:text-signal-protocol"
            >
              + {s}
            </button>
          ))}
        </div>
      )}
    </FormField>
  );
}

// ---------------------------------------------------------------------------
// The run view — the build theater. Streams the coordinator's crew events and
// hands the deploy phase to the live deployment timeline (with its log drawer).
// ---------------------------------------------------------------------------

const PHASES: { id: string; label: string; actor: StudioRunEvent["actor"] }[] = [
  { id: "building", label: "Build", actor: "builder" },
  { id: "evaluating", label: "Evaluate", actor: "builder" },
  { id: "reviewing", label: "Review", actor: "reviewer" },
  { id: "improving", label: "Improve", actor: "editor" },
  { id: "deploying", label: "Deploy", actor: "deployer" },
];

const TERMINAL_STATUSES = new Set(["live", "failed"]);

function StudioRunView({ runId }: { runId: string }) {
  const navigate = useNavigate();
  const [run, setRun] = useState<StudioRun | null>(null);
  const [deployment, setDeployment] = useState<AgentDeployment | null>(null);
  const [unavailable, setUnavailable] = useState<string | null>(null);

  // Initial load + live stream.
  useEffect(() => {
    let active = true;
    const controller = new AbortController();
    (async () => {
      try {
        const initial = await getStudioRun(runId);
        if (!active) return;
        setRun(initial);
      } catch (err) {
        if (!active) return;
        setUnavailable(
          err instanceof Error ? err.message : "This run is not available yet.",
        );
        return;
      }
      try {
        for await (const ev of streamStudioRun(runId, controller.signal)) {
          if (!active) return;
          if (ev.type === "snapshot" || ev.type === "done") {
            if (ev.run) setRun(ev.run);
          } else if (ev.type === "event") {
            setRun((prev) =>
              prev ? { ...prev, events: mergeEvent(prev.events, ev.event) } : prev,
            );
          }
        }
      } catch {
        /* stream ended or aborted; snapshot state stands */
      }
    })();
    return () => {
      active = false;
      controller.abort();
    };
  }, [runId]);

  // Pull the deployment once the run exposes a deploy id, so the timeline (and
  // its build/pod/argo log drawer) renders the deploy phase.
  const deployId = run?.deploy_id ?? run?.report?.deploy_id ?? null;
  const agentName = run?.agent_name ?? run?.report?.agent_name ?? null;
  useEffect(() => {
    if (!deployId || !agentName) return;
    let active = true;
    getAgentDeployment(agentName, deployId)
      .then((d) => active && setDeployment(d))
      .catch(() => {});
    return () => {
      active = false;
    };
  }, [deployId, agentName]);

  if (unavailable) {
    return (
      <div className="mx-auto max-w-2xl space-y-4 p-1">
        <InlineAlert tone="amber" className="text-sm">
          <div className="font-semibold text-signal-authority">Run not available yet</div>
          <p className="mt-1 text-ink-muted">{unavailable}</p>
        </InlineAlert>
        <ToolbarButton onClick={() => navigate("/studio")}>← Start a new build</ToolbarButton>
      </div>
    );
  }

  if (!run) {
    return <div className="p-4 text-sm text-ink-muted">Connecting to your build…</div>;
  }

  const currentIndex = PHASES.findIndex((p) => p.id === run.status);
  const isLive = run.status === "live";

  return (
    <div className="mx-auto max-w-4xl space-y-4 p-1">
      <RunHeader run={run} onNew={() => navigate("/studio")} />
      <CrewStrip run={run} currentIndex={currentIndex} />

      <SurfacePanel as="section" className="p-4">
        <div className="text-[10px] uppercase tracking-[0.18em] text-ink-faint">
          Build theater
        </div>
        <ol className="mt-3 space-y-3">
          {PHASES.map((phase, index) => (
            <PhaseRow
              key={phase.id}
              phase={phase}
              index={index}
              currentIndex={currentIndex}
              runStatus={run.status}
              events={run.events.filter((e) => e.phase === phase.id)}
              deployment={phase.id === "deploying" ? deployment : null}
            />
          ))}
        </ol>
      </SurfacePanel>

      {isLive && run.report && <PayoffCard run={run} />}
    </div>
  );
}

function mergeEvent(events: StudioRunEvent[], next: StudioRunEvent): StudioRunEvent[] {
  if (events.some((e) => e.id === next.id)) {
    return events.map((e) => (e.id === next.id ? next : e));
  }
  return [...events, next];
}

function RunHeader({ run, onNew }: { run: StudioRun; onNew: () => void }) {
  const spent = (run.budget_spent_cents / 100).toFixed(2);
  const cap = (run.brief.budget_cents / 100).toFixed(2);
  return (
    <header className="flex flex-wrap items-start justify-between gap-3">
      <div className="min-w-0">
        <div className="text-[10px] uppercase tracking-[0.18em] text-ink-faint">
          Agent Studio · run
        </div>
        <h1 className="mt-1 flex items-center gap-2 text-xl font-semibold text-ink">
          <span className="font-mono">{run.agent_name}</span>
          <RunStatusBadge status={run.status} />
        </h1>
        <p className="mt-1 max-w-2xl text-sm leading-relaxed text-ink-dim">{run.brief.goal}</p>
      </div>
      <div className="flex items-center gap-2">
        <div className="rounded-lg border border-runtime-line-soft/60 bg-runtime-panel px-3 py-1.5 text-right">
          <div className="font-mono text-[10px] uppercase tracking-wide text-ink-faint">Budget</div>
          <div className="font-mono text-sm tabular-nums text-signal-authority">
            ${spent}
            <span className="text-ink-faint"> / {cap}</span>
          </div>
        </div>
        <ToolbarButton onClick={onNew}>New build</ToolbarButton>
      </div>
    </header>
  );
}

function CrewStrip({ run, currentIndex }: { run: StudioRun; currentIndex: number }) {
  const members: { actor: StudioRunEvent["actor"]; glyph: string; name: string }[] = [
    { actor: "builder", glyph: "◆", name: "Builder" },
    { actor: "reviewer", glyph: "⬡", name: "Reviewer" },
    { actor: "editor", glyph: "✎", name: "Editor" },
    { actor: "deployer", glyph: "▲", name: "Deployer" },
  ];
  return (
    <div className="flex flex-wrap gap-2">
      {members.map((m) => {
        const last = [...run.events].reverse().find((e) => e.actor === m.actor);
        const phaseIndex = PHASES.findIndex((p) => p.actor === m.actor);
        const active = phaseIndex === currentIndex && !TERMINAL_STATUSES.has(run.status);
        return (
          <div
            key={m.actor}
            className={`flex min-w-40 flex-1 items-center gap-2.5 rounded-xl border bg-runtime-soft px-3 py-2 ${
              active ? "border-runtime-line-mid" : "border-runtime-line-soft/60"
            }`}
          >
            <span className="grid h-8 w-8 flex-none place-items-center rounded-lg bg-runtime-raised text-sm text-signal-protocol">
              {m.glyph}
            </span>
            <div className="min-w-0">
              <div className="text-xs font-semibold text-ink">{m.name}</div>
              <div className={`truncate font-mono text-[10.5px] ${active ? "text-signal-protocol" : "text-ink-muted"}`}>
                {last?.status === "running" || active ? "working…" : last?.message || "queued"}
              </div>
            </div>
          </div>
        );
      })}
    </div>
  );
}

function PhaseRow({
  phase,
  index,
  currentIndex,
  runStatus,
  events,
  deployment,
}: {
  phase: { id: string; label: string };
  index: number;
  currentIndex: number;
  runStatus: string;
  events: StudioRunEvent[];
  deployment: AgentDeployment | null;
}) {
  const failed = runStatus === "failed" && index === currentIndex;
  const done = runStatus === "live" ? true : index < currentIndex;
  const now = index === currentIndex && !TERMINAL_STATUSES.has(runStatus);
  const node = failed ? "bg-signal-danger" : done ? "bg-signal-live" : now ? "bg-signal-protocol" : "bg-runtime-line";
  const latest = events[events.length - 1];

  return (
    <li className="grid grid-cols-[20px_1fr] gap-3">
      <div className="flex flex-col items-center">
        <span className={`mt-1 h-3.5 w-3.5 flex-none rounded-full ${node}`} />
        {index < PHASES.length - 1 && <span className="my-1 w-0.5 flex-1 bg-runtime-line-soft" />}
      </div>
      <div className="min-w-0 pb-1">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-sm font-semibold text-ink-soft">{phase.label}</span>
          {done && <StatusBadge tone="emerald" className="min-h-4 px-1.5 text-[10px]">done</StatusBadge>}
          {now && <StatusBadge tone="amber" className="min-h-4 px-1.5 text-[10px]">running</StatusBadge>}
          {failed && <StatusBadge tone="red" className="min-h-4 px-1.5 text-[10px]">failed</StatusBadge>}
        </div>
        {events.map((e) => (
          <div key={e.id} className="mt-1 text-xs leading-relaxed text-ink-muted">
            {e.status === "failed" && <span className="text-signal-danger">✕ </span>}
            {e.message}
          </div>
        ))}
        {!events.length && !now && !done && (
          <div className="mt-1 text-xs text-ink-faint">Waiting.</div>
        )}
        {phase.id === "improving" && latest?.status === "passed" && (
          <div className="mt-2 rounded-lg border border-signal-peer/30 bg-signal-peer/5 px-3 py-2 text-xs text-ink-soft">
            <span className="font-mono text-signal-peer">↻ self-repair</span> — the reviewer's
            findings were fixed and re-verified before deploy.
          </div>
        )}
        {phase.id === "deploying" && deployment && (
          <div className="mt-2">
            <AgentDeploymentTimeline
              deployment={deployment}
              agent={{ name: deployment.agent_name }}
              title="Deploy"
              eventLimit={4}
            />
          </div>
        )}
      </div>
    </li>
  );
}

function PayoffCard({ run }: { run: StudioRun }) {
  const report = run.report!;
  return (
    <SurfacePanel as="section" className="overflow-hidden border-signal-live/30 p-0">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-runtime-line-soft/60 bg-signal-live/5 px-4 py-3">
        <h2 className="flex items-center gap-2 text-lg font-semibold text-ink">
          <span className="text-signal-live">✦</span> Your agent is live
        </h2>
        <RunStatusBadge status={run.status} />
      </div>
      <div className="grid gap-0 sm:grid-cols-2">
        <dl className="space-y-3 p-4">
          {report.agent_url && (
            <HandoffRow label="Live URL">
              <a href={report.agent_url} target="_blank" rel="noreferrer" className="text-signal-protocol hover:underline">
                {report.agent_url.replace(/^https?:\/\//, "")}
              </a>
              <CopyButton value={report.agent_url} label="Copy URL" className="ml-auto h-5 px-1.5 text-[10px]">
                copy
              </CopyButton>
            </HandoffRow>
          )}
          {report.mcp_url && (
            <HandoffRow label="MCP connector">
              <span className="truncate">{report.mcp_url}</span>
              <CopyButton value={report.mcp_url} label="Copy MCP URL" className="ml-auto h-5 px-1.5 text-[10px]">
                copy
              </CopyButton>
            </HandoffRow>
          )}
          {report.public_url && (
            <HandoffRow label="Public page">
              <a href={report.public_url} target="_blank" rel="noreferrer" className="truncate text-signal-protocol hover:underline">
                {report.public_url.replace(/^https?:\/\//, "")}
              </a>
            </HandoffRow>
          )}
          {report.source_url && (
            <HandoffRow label="Source">
              <a href={report.source_url} target="_blank" rel="noreferrer" className="truncate text-signal-protocol hover:underline">
                public repository ↗
              </a>
            </HandoffRow>
          )}
          {report.cli && (
            <HandoffRow label="CLI">
              <span className="truncate">{report.cli}</span>
              <CopyButton value={report.cli} label="Copy CLI command" className="ml-auto h-5 px-1.5 text-[10px]">copy</CopyButton>
            </HandoffRow>
          )}
          <HandoffRow label="Proof">
            <span className="text-signal-proof">
              ◈ {report.tests_passed}/{report.tests_total} checks passed
              {report.receipt_id ? " · signed receipt" : ""}
            </span>
          </HandoffRow>
        </dl>
        <div className="space-y-2 border-t border-runtime-line-soft/60 p-4 sm:border-l sm:border-t-0">
          <div className="text-[10px] uppercase tracking-[0.18em] text-ink-faint">Next</div>
          <div className="flex flex-wrap gap-2">
            {(report.next_actions.length ? report.next_actions : ["Publish to marketplace", "Add to a team", "Give it a schedule"]).map(
              (action) => (
                <button
                  key={action}
                  type="button"
                  className="rounded-lg border border-runtime-line bg-runtime-raised px-3 py-1.5 text-xs text-ink-soft hover:border-signal-protocol hover:text-signal-protocol"
                >
                  {action}
                </button>
              ),
            )}
          </div>
        </div>
      </div>
    </SurfacePanel>
  );
}

function HandoffRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="space-y-1">
      <dt className="font-mono text-[10px] uppercase tracking-[0.13em] text-ink-faint">{label}</dt>
      <dd className="flex items-center gap-2 rounded-md border border-runtime-line-soft/60 bg-runtime-bg/40 px-2.5 py-1.5 font-mono text-xs text-ink-soft">
        {children}
      </dd>
    </div>
  );
}

function RunStatusBadge({ status }: { status: string }) {
  const tone: StatusBadgeTone =
    status === "live" ? "emerald" : status === "failed" ? "red" : "amber";
  return (
    <StatusBadge tone={tone} dot={!TERMINAL_STATUSES.has(status)}>
      {status}
    </StatusBadge>
  );
}
