/**
 * Shared types + pure helpers for the bounty lifecycle surface.
 *
 * Extracted from the former 1187-LOC BountyLifecyclePanel (mandate D: keep
 * files small and single-purpose). The lifecycle summary computation and the
 * formatting/derivation helpers live here so the EvidenceSummary and
 * ActionsSummary sub-panels can import them without depending on each other
 * or on the orchestrating panel.
 *
 * NOTE: no business-logic / network / data-shape changes vs the original —
 * this is a pure relocation of the existing functions.
 */
import type {
  AgentListing,
  Bounty,
  BountyStatus,
  TrialRoom,
  TrialRoomDraft,
  TrialRun,
} from "../api";

export type BountyLifecycleAction =
  | "create-trial"
  | "claim"
  | "fulfill"
  | "cancel";

export type BountyLifecycleView = "overview" | "evidence" | "actions";

type BountyLifecycleRole = "owner" | "claimant" | "viewer";

export type BountyLifecycleGuidanceTone =
  | "neutral"
  | "amber"
  | "emerald"
  | "red";

export type BountyLifecycleGuidance = {
  id: string;
  title: string;
  detail: string;
  tone: BountyLifecycleGuidanceTone;
};

export type BountyLifecycleSummary = {
  role: BountyLifecycleRole;
  isOwner: boolean;
  claimedByMyAgent: boolean;
  hasClaimedAgent: boolean;
  terminal: boolean;
  claimable: boolean;
  canFulfill: boolean;
  canCancel: boolean;
  eligibleClaimAgents: AgentListing[];
  linkedTrialRooms: TrialRoom[];
  claimedAgentRuns: TrialRun[];
  selectedClaimedRuns: TrialRun[];
  passedClaimedRuns: TrialRun[];
  bestClaimedRun: TrialRun | null;
  latestClaimedRun: TrialRun | null;
  evidence: {
    linkedTrials: number;
    agentRuns: number;
    passedRuns: number;
    selectedRuns: number;
    receipts: number;
    changedFiles: number;
    bestScore: number | null;
    latestStatus: string | null;
  };
  guidance: BountyLifecycleGuidance[];
  trialDraft: TrialRoomDraft;
};

export type BountyLifecyclePanelActions =
  | {
      onCreateTrial?: (draft: TrialRoomDraft, bounty: Bounty) => void | Promise<void>;
      onClaim?: (agentName: string, bounty: Bounty) => void | Promise<void>;
      onFulfill?: (bounty: Bounty) => void | Promise<void>;
      onCancel?: (bounty: Bounty) => void | Promise<void>;
    }
  | undefined;

export type ToneClasses = {
  border: string;
  bg: string;
  text: string;
  marker: string;
};

export const STATUS_ORDER: readonly BountyStatus[] = [
  "open",
  "claimed",
  "fulfilled",
  "cancelled",
];

export const EMPTY_AGENTS: readonly AgentListing[] = [];
export const EMPTY_TRIAL_ROOMS: readonly TrialRoom[] = [];

export const GUIDANCE_TONES: Record<BountyLifecycleGuidanceTone, ToneClasses> = {
  neutral: {
    border: "border-runtime-line-soft/60",
    bg: "bg-runtime-panel/40",
    text: "text-ink-soft",
    marker: "bg-ink-muted",
  },
  amber: {
    border: "border-signal-authority/45",
    bg: "bg-signal-authority/12",
    text: "text-signal-authority",
    marker: "bg-signal-authority",
  },
  emerald: {
    border: "border-signal-live/45",
    bg: "bg-signal-live/12",
    text: "text-signal-live",
    marker: "bg-signal-live",
  },
  red: {
    border: "border-signal-danger/50",
    bg: "bg-signal-danger/12",
    text: "text-signal-danger",
    marker: "bg-signal-danger",
  },
};

export function summarizeBountyLifecycle(
  bounty: Bounty,
  options: {
    me?: string | null;
    myAgents?: readonly AgentListing[];
    trialRooms?: readonly TrialRoom[];
  } = {},
): BountyLifecycleSummary {
  const me = options.me || "";
  const myAgents = [...(options.myAgents || [])];
  const linkedTrialRooms = findBountyTrialRooms(
    bounty,
    options.trialRooms || [],
  );
  const claimedAgentRuns = bounty.claimed_agent_name
    ? findAgentTrialRuns(bounty.claimed_agent_name, options.trialRooms || [])
    : [];
  const selectedRunIds = new Set(
    linkedTrialRooms
      .map((room) => room.selected_run_id)
      .filter((runId): runId is number => runId !== null),
  );
  const selectedClaimedRuns = claimedAgentRuns.filter((run) =>
    selectedRunIds.has(run.id),
  );
  const passedClaimedRuns = claimedAgentRuns.filter((run) =>
    isPassedRun(run),
  );
  const eligibleClaimAgents = myAgents.filter(isEligibleClaimAgent);
  const claimedByMyAgent = Boolean(
    bounty.claimed_agent_name &&
      myAgents.some((agent) => agent.name === bounty.claimed_agent_name),
  );
  const isOwner = Boolean(me && bounty.posted_by_email === me);
  const role: BountyLifecycleRole = isOwner
    ? "owner"
    : claimedByMyAgent
      ? "claimant"
      : "viewer";
  const terminal = bounty.status === "fulfilled" || bounty.status === "cancelled";
  const claimable =
    !isOwner &&
    !terminal &&
    (bounty.status === "open" || bounty.status === "claimed") &&
    eligibleClaimAgents.length > 0;
  const bestClaimedRun = claimedAgentRuns[0] || null;
  const latestClaimedRun = latestTrialRun(claimedAgentRuns);
  const evidence = {
    linkedTrials: linkedTrialRooms.length,
    agentRuns: claimedAgentRuns.length,
    passedRuns: passedClaimedRuns.length,
    selectedRuns: selectedClaimedRuns.length,
    receipts: claimedAgentRuns.filter((run) => receiptId(run)).length,
    changedFiles: claimedAgentRuns.reduce(
      (sum, run) => sum + changedFileCount(run),
      0,
    ),
    bestScore: bestClaimedRun ? bestClaimedRun.score : null,
    latestStatus: latestClaimedRun?.status ?? null,
  };
  const summaryBase = {
    role,
    isOwner,
    claimedByMyAgent,
    hasClaimedAgent: Boolean(bounty.claimed_agent_name),
    terminal,
    claimable,
    canFulfill: isOwner && bounty.status === "claimed",
    canCancel: isOwner && !terminal,
    eligibleClaimAgents,
    linkedTrialRooms,
    claimedAgentRuns,
    selectedClaimedRuns,
    passedClaimedRuns,
    bestClaimedRun,
    latestClaimedRun,
    evidence,
    trialDraft: trialDraftFromBounty(bounty),
  };

  return {
    ...summaryBase,
    guidance: buildGuidance(bounty, summaryBase),
  };
}

function findBountyTrialRooms(
  bounty: Bounty,
  rooms: readonly TrialRoom[],
): TrialRoom[] {
  const slug = bounty.slug.toLowerCase();
  const expectedTitle = trialTitleFromBounty(bounty).toLowerCase();
  return rooms
    .filter((room) => {
      const haystack = [
        room.slug,
        room.title,
        room.goal,
        room.acceptance_criteria,
      ]
        .join("\n")
        .toLowerCase();
      return haystack.includes(slug) || room.title.toLowerCase() === expectedTitle;
    })
    .sort(compareRoomsByUpdatedAt);
}

function findAgentTrialRuns(
  agentName: string,
  rooms: readonly TrialRoom[],
): TrialRun[] {
  return rooms
    .flatMap((room) => room.runs)
    .filter((run) => run.agent_name === agentName)
    .sort(compareRunsByScoreThenDate);
}

function trialDraftFromBounty(bounty: Bounty): TrialRoomDraft {
  const goal = [
    `Bounty ${bounty.slug}: ${bounty.title}`,
    bounty.description,
    bounty.example_input ? `Example input:\n${bounty.example_input}` : "",
    bounty.claimed_agent_name
      ? `Claimed agent: ${bounty.claimed_agent_name}`
      : "",
  ]
    .filter(Boolean)
    .join("\n\n");
  const acceptance = [
    bounty.example_output ? `Expected output:\n${bounty.example_output}` : "",
    bounty.claimed_agent_name
      ? `Compare ${bounty.claimed_agent_name} against at least one alternate agent before fulfillment.`
      : "Compare candidate agents before fulfillment.",
  ]
    .filter(Boolean)
    .join("\n\n");

  return {
    title: trialTitleFromBounty(bounty),
    goal,
    acceptance_criteria: acceptance,
    input_paths: [],
    output_schema: schemaFromExampleOutput(bounty.example_output),
    max_runtime_seconds: 300,
  };
}

function buildGuidance(
  bounty: Bounty,
  summary: Omit<BountyLifecycleSummary, "guidance">,
): BountyLifecycleGuidance[] {
  if (bounty.status === "cancelled") {
    return [
      {
        id: "cancelled",
        title: "Bounty is cancelled",
        detail: "This is terminal; do not claim, fulfill, or create new evidence for payment.",
        tone: "neutral",
      },
    ];
  }

  if (bounty.status === "fulfilled") {
    return [
      {
        id: "fulfilled",
        title: "Fulfillment complete",
        detail: "Keep the linked trial receipts and selected run available as fulfillment evidence.",
        tone: "emerald",
      },
    ];
  }

  const guidance: BountyLifecycleGuidance[] = [];

  if (bounty.status === "open") {
    guidance.push(
      summary.isOwner
        ? {
            id: "owner-open",
            title: "Waiting for a claim",
            detail: "Create a trial room now if acceptance criteria should be locked before agents compete.",
            tone: "neutral",
          }
        : summary.claimable
          ? {
              id: "claim-ready",
              title: "Ready to claim",
              detail: "Use a public running agent, then produce trial evidence before asking for fulfillment.",
              tone: "amber",
            }
          : {
              id: "claim-blocked",
              title: "Claim requires an eligible agent",
              detail: "Only public running agents can claim this bounty from this surface.",
              tone: "amber",
            },
    );
  }

  if (bounty.status === "claimed") {
    if (summary.isOwner) {
      guidance.push({
        id: "owner-review",
        title: "Review evidence before fulfillment",
        detail:
          summary.evidence.agentRuns > 0
            ? "Check the claimed agent runs, scores, receipts, and selected winner before marking the bounty fulfilled."
            : "Ask the claimant to run the claimed agent in a linked trial before paying.",
        tone: summary.evidence.agentRuns > 0 ? "emerald" : "amber",
      });
    } else if (summary.claimedByMyAgent) {
      guidance.push({
        id: "claimant-evidence",
        title: "Evidence is your path to payment",
        detail: "Run the claimed agent in a linked trial and keep receipts, outputs, and evaluator notes attached.",
        tone: "amber",
      });
    } else {
      guidance.push({
        id: "claimed-by-other",
        title: "Claim is already assigned",
        detail: "Another agent has the active claim. Use trial evidence to compare alternatives if the owner reopens the work.",
        tone: "neutral",
      });
    }
  }

  if (summary.evidence.linkedTrials === 0) {
    guidance.push({
      id: "trial-missing",
      title: "No linked trial yet",
      detail: "A trial room gives the bounty a private evidence trail for inputs, outputs, scores, and receipts.",
      tone: "amber",
    });
  } else if (summary.evidence.selectedRuns === 0) {
    guidance.push({
      id: "winner-missing",
      title: "No selected trial winner",
      detail: "Select a winning run in the trial room so fulfillment can point to one receipt.",
      tone: "amber",
    });
  } else {
    guidance.push({
      id: "winner-ready",
      title: "Selected run available",
      detail: "The linked trial has a selected run that can anchor fulfillment review.",
      tone: "emerald",
    });
  }

  return guidance;
}

function isEligibleClaimAgent(agent: AgentListing): boolean {
  return agent.public && agent.status === "running";
}

function isPassedRun(run: TrialRun): boolean {
  return run.status === "passed" || run.score >= 70;
}

function latestTrialRun(runs: readonly TrialRun[]): TrialRun | null {
  let latest: TrialRun | null = null;
  for (const run of runs) {
    if (!latest || timestamp(run.created_at) > timestamp(latest.created_at)) {
      latest = run;
    }
  }
  return latest;
}

function compareRunsByScoreThenDate(a: TrialRun, b: TrialRun): number {
  return (
    b.score - a.score ||
    timestamp(b.completed_at || b.started_at || b.created_at) -
      timestamp(a.completed_at || a.started_at || a.created_at) ||
    b.id - a.id
  );
}

function compareRoomsByUpdatedAt(a: TrialRoom, b: TrialRoom): number {
  return timestamp(b.updated_at) - timestamp(a.updated_at) || b.id - a.id;
}

export function changedFileCount(run: TrialRun): number {
  return run.file_ops.filter((op) => op.op === "create" || op.op === "update")
    .length;
}

export function receiptId(run: TrialRun): string {
  const value = run.receipt_json["receipt_id"];
  return typeof value === "string" ? value : "";
}

function trialTitleFromBounty(bounty: Bounty): string {
  return `Bounty: ${bounty.title}`.slice(0, 180);
}

function schemaFromExampleOutput(
  exampleOutput: string,
): Record<string, unknown> {
  const fallback = {
    type: "object",
    required: ["summary"],
    properties: {
      summary: { type: "string" },
    },
  };
  if (!exampleOutput.trim()) return fallback;
  try {
    const parsed: unknown = JSON.parse(exampleOutput);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      return fallback;
    }
    const record = parsed as Record<string, unknown>;
    const keys = Object.keys(record);
    if (keys.length === 0) return fallback;
    return {
      type: "object",
      required: keys,
      properties: Object.fromEntries(
        keys.map((key) => [key, { type: jsonSchemaType(record[key]) }]),
      ),
    };
  } catch {
    return fallback;
  }
}

function jsonSchemaType(value: unknown): string {
  if (Array.isArray(value)) return "array";
  if (value === null) return "null";
  const kind = typeof value;
  return kind === "number" || kind === "boolean" || kind === "string"
    ? kind
    : "object";
}

export function skillsFromBountyCard(
  card: Record<string, unknown> | null,
): string[] {
  const skills = card?.["skills"];
  if (!Array.isArray(skills)) return [];
  return skills
    .map((skill) => {
      if (!skill || typeof skill !== "object") return "";
      const name = (skill as Record<string, unknown>)["name"];
      return typeof name === "string" ? name : "";
    })
    .filter(Boolean);
}

export function formatMs(ms: number | null): string {
  if (ms == null) return "-";
  return ms < 1000 ? `${ms}ms` : `${(ms / 1000).toFixed(1)}s`;
}

export function formatDate(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "-";
  return date.toLocaleDateString();
}

export function formatDateTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "-";
  return date.toLocaleString();
}

function timestamp(value: string | null): number {
  if (!value) return 0;
  const parsed = Date.parse(value);
  return Number.isNaN(parsed) ? 0 : parsed;
}

export function bountyStatusTone(status: BountyStatus): StatusBadgeTone {
  if (status === "fulfilled") return "emerald";
  if (status === "claimed") return "amber";
  if (status === "open") return "neutral";
  if (status === "cancelled") return "red";
  return "neutral";
}

// Re-exported here so sub-panels can type the shared tone helper without a
// second import path. Kept as a type-only import to avoid value coupling.
import type { StatusBadgeTone } from "./DashboardChrome";
export type { StatusBadgeTone };
