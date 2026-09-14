import { memo, useCallback, useMemo, useState } from "react";
import {
  approveHandoff,
  rerunSubagentRun,
  type ChatEvent,
  type ChatMessage,
  type HandoffScopes,
} from "../api";
import { runtimeControlReceiptHref } from "../navigation";
import { findChatEvidenceCards } from "../chatEvidence";
import { ChatEvidenceCards, LiveEvidenceCard } from "./ChatEvidenceCards";
import { ChatFileOpsList, type ChatFileOp } from "./ChatFileOpsList";
import {
  ChatAgentSetupRequiredCard,
  type AgentSetupRequiredEvent,
} from "./ChatAgentSetupRequiredCard";
import type { ChatConnectionState } from "./ChatConnectionStatus";
import { ChatMessageRow, ChatToolChip } from "./ChatTranscript";
import {
  QuestionCard,
  ScopeCard,
  type QuestionEvent,
  type ScopeEvent,
} from "./ChatApprovalCards";
import { InlineAlert, SummaryMetric, ToolbarButton, ToolbarLink } from "./DashboardChrome";
import {
  ChatInputRequestCard,
  type ChatInputRequestEvent as InputRequestEvent,
} from "./ChatInputRequestCard";
import { Icon } from "./Icon";
import { useOptionalWorkspaceInspector } from "../providers/WorkspaceInspectorContext";

export type ProgressEntry = { kind: string; message: string };

type HandoffEvent = {
  kind: "handoff";
  from: string;
  to: string;
  skill: string;
  grant_id: string;
  scopes: HandoffScopes;
  args_preview: Record<string, unknown>;
  needs_approval: boolean;
  approval_id?: string;
  status: "proposed" | "running" | "complete" | "denied" | "error";
  summary?: string;
  file_ops?: ChatFileOp[];
  progress: ProgressEntry[];
};

type ToolEvent = {
  kind: "tool";
  id: string;
  tool: string;
  args_preview: Record<string, unknown>;
  status: "running" | "ok" | "error";
  summary?: string;
};

type DagNodeItem = {
  node_id: string;
  agent: string;
  skill: string;
  deps: string[];
  args_preview: Record<string, unknown>;
  status: "pending" | "running" | "complete" | "error" | "skipped";
  summary?: string;
  error?: string | null;
  result?: Record<string, unknown>;
  grant_id?: string | null;
  file_ops?: ChatFileOp[];
  elapsed_ms?: number | null;
  started_at?: string | null;
  completed_at?: string | null;
};

type DagEvent = {
  kind: "dag";
  dag_run_id: string;
  goal: string;
  status: "running" | "complete" | "error";
  summary?: string;
  nodes: DagNodeItem[];
};

type ReviewLoopChatStep = {
  event_type: string;
  status?: string | null;
  severity?: string | null;
  message?: string | null;
  payload?: Record<string, unknown>;
};

type ReviewLoopChatEvent = {
  kind: "review_loop";
  job_id: string;
  agent: string;
  status: "queued" | "running" | "blocked" | "complete" | "failed" | "killed" | string;
  summary?: string | null;
  critical_findings: number;
  proposed_fixes: number;
  promotion_frozen: boolean;
  events: ReviewLoopChatStep[];
};

type WriteScopeLike = {
  outputs_prefix?: string | null;
  write_prefix?: string | null;
  write_prefixes?: string[] | null;
};

function writeScopeLabels(scope: WriteScopeLike): string[] {
  const labels = Array.isArray(scope.write_prefixes)
    ? scope.write_prefixes.filter(Boolean)
    : [];
  if (labels.length === 0 && scope.write_prefix) labels.push(scope.write_prefix);
  if (labels.length === 0 && scope.outputs_prefix) labels.push(scope.outputs_prefix);
  return labels;
}

type EvidenceStreamEvent = Extract<ChatEvent, { type: "evidence_event" }>;

type MessageItemBody = { kind: "msg"; role: ChatMessage["role"]; content: string };

type ActivityItemBody =
  | HandoffEvent
  | DagEvent
  | ReviewLoopChatEvent
  | ToolEvent
  | QuestionEvent
  | InputRequestEvent
  | ScopeEvent
  | { kind: "evidence"; event: EvidenceStreamEvent }
  | AgentSetupRequiredEvent;

type ItemBody = MessageItemBody | ActivityItemBody;
type MessageItem = MessageItemBody & { id: string };
type ActivityItem = ActivityItemBody & { id: string };
export type Item = MessageItem | ActivityItem;

let nextItemId = 0;
function newItemId(): string {
  nextItemId += 1;
  return `item-${nextItemId}`;
}

function itemOrder(item: Item): number {
  const match = /^item-(\d+)$/.exec(item.id);
  return match ? Number(match[1]) : 0;
}

const PROGRESS_TRAIL_LIMIT = 12;
const HIDDEN_TOOL_CHIPS = new Set([
  "call_agent",
  "plan_agent_dag",
  "execute_agent_dag",
]);

function evidenceAlreadyRenderedByStructuredItem(
  items: ActivityItem[],
  event: EvidenceStreamEvent,
): boolean {
  if (!["arena_suite", "scenario_trace"].includes(event.evidence_kind)) {
    return false;
  }
  const source = event.source || {};
  if (source.kind === "review_loop") {
    const jobId = typeof source.job_id === "string" ? source.job_id : null;
    return Boolean(
      jobId &&
        items.some((item) => item.kind === "review_loop" && item.job_id === jobId),
    );
  }
  if (source.kind === "dag_node") {
    const dagRunId = typeof source.dag_run_id === "string" ? source.dag_run_id : null;
    const nodeId = typeof source.node_id === "string" ? source.node_id : null;
    return Boolean(
      dagRunId &&
        nodeId &&
        items.some(
          (item) =>
            item.kind === "dag" &&
            item.dag_run_id === dagRunId &&
            item.nodes.some((node) => node.node_id === nodeId && node.result),
        ),
    );
  }
  return false;
}

export function appendProgressEntry(
  entries: ProgressEntry[],
  entry: ProgressEntry,
  limit = PROGRESS_TRAIL_LIMIT,
): ProgressEntry[] {
  const last = entries[entries.length - 1];
  if (last?.kind === entry.kind && last.message === entry.message) return entries;
  return [...entries, entry].slice(-limit);
}

type DispatchEventOptions = { replay?: boolean };

/**
 * Side-effects the event dispatcher reports back to the stream controller — the
 * chat-level connection state and the human-readable stream status string.
 */
export type ChatEventEffects = {
  onConnectionState: (state: ChatConnectionState) => void;
  onStreamStatus: (status: string, replay: boolean) => void;
  onAssistantDelta: (content: string) => void;
  onAssistantFinal: (content: string) => void;
  onError: (message: string) => void;
};

export type ChatEventDispatcher = {
  messages: MessageItem[];
  activityItems: ActivityItem[];
  items: Item[];
  /**
   * Hydrate a thread's persisted message history: clears all items and seeds
   * the message list with freshly-ordered ids. Replayed activity events should
   * be dispatched (with `{ replay: true }`) immediately afterwards.
   */
  loadHistory: (history: Array<{ role: ChatMessage["role"]; content: string }>) => void;
  /** Append a fully-formed item (message or activity). */
  pushItem: (body: ItemBody) => void;
  /** Clear all messages and activity (thread switch / new chat). */
  resetItems: () => void;
  /** Apply one streamed/replayed chat event to the item state. */
  dispatchEvent: (ev: ChatEvent, options?: DispatchEventOptions) => void;
};

/**
 * useChatEventDispatcher — the chat transcript reducer. Owns the message and
 * activity-item state and translates every {@link ChatEvent} into item
 * mutations (tool chips, handoff/DAG/review-loop cards, scope/input/question
 * approvals, evidence). Connection-state and status side-effects are delegated
 * to the stream controller via {@link ChatEventEffects} so this module stays a
 * pure transcript reducer.
 */
export function useChatEventDispatcher(effects: ChatEventEffects): ChatEventDispatcher {
  const [messages, setMessages] = useState<MessageItem[]>([]);
  const [activityItems, setActivityItems] = useState<ActivityItem[]>([]);
  const items = useMemo<Item[]>(
    () => [...messages, ...activityItems].sort((a, b) => itemOrder(a) - itemOrder(b)),
    [activityItems, messages],
  );

  const pushItem = useCallback((it: ItemBody) => {
    const id = newItemId();
    if (it.kind === "msg") {
      setMessages((m) => [...m, { ...it, id }]);
    } else {
      setActivityItems((m) => [...m, { ...it, id }]);
    }
  }, []);

  const resetItems = useCallback(() => {
    setMessages([]);
    setActivityItems([]);
  }, []);

  const loadHistory = useCallback(
    (history: Array<{ role: ChatMessage["role"]; content: string }>) => {
      setMessages(
        history.map((m) => ({
          id: newItemId(),
          kind: "msg" as const,
          role: m.role,
          content: m.content,
        })),
      );
      setActivityItems([]);
    },
    [],
  );

  function patchHandoff(grant_id: string, patch: Partial<HandoffEvent>) {
    setActivityItems((m) =>
      m.map((it) =>
        it.kind === "handoff" && it.grant_id === grant_id ? { ...it, ...patch } : it,
      ),
    );
  }

  function patchTool(id: string, patch: Partial<ToolEvent>) {
    setActivityItems((m) =>
      m.map((it) => (it.kind === "tool" && it.id === id ? { ...it, ...patch } : it)),
    );
  }

  function patchDag(dag_run_id: string, patch: Partial<DagEvent>) {
    setActivityItems((m) =>
      m.map((it) =>
        it.kind === "dag" && it.dag_run_id === dag_run_id ? { ...it, ...patch } : it,
      ),
    );
  }

  function patchDagNode(
    dag_run_id: string,
    node_id: string,
    patch: Partial<DagNodeItem>,
  ) {
    setActivityItems((m) =>
      m.map((it) => {
        if (it.kind !== "dag" || it.dag_run_id !== dag_run_id) return it;
        let found = false;
        const nodes = it.nodes.map((node) => {
          if (node.node_id !== node_id) return node;
          found = true;
          return { ...node, ...patch };
        });
        if (!found) {
          nodes.push({
            node_id,
            agent: patch.agent || "",
            skill: patch.skill || "",
            deps: patch.deps || [],
            args_preview: patch.args_preview || {},
            status: patch.status || "pending",
            summary: patch.summary,
            error: patch.error,
            result: patch.result,
            grant_id: patch.grant_id,
            file_ops: patch.file_ops,
            elapsed_ms: patch.elapsed_ms,
            started_at: patch.started_at,
            completed_at: patch.completed_at,
          });
        }
        return { ...it, nodes };
      }),
    );
  }

  function patchScope(request_id: string, patch: Partial<ScopeEvent>) {
    setActivityItems((m) =>
      m.map((it) =>
        it.kind === "scope" && it.request_id === request_id ? { ...it, ...patch } : it,
      ),
    );
  }

  function upsertEvidenceEvent(event: EvidenceStreamEvent) {
    setActivityItems((m) => {
      if (evidenceAlreadyRenderedByStructuredItem(m, event)) return m;
      let found = false;
      const next = m.map((item) => {
        if (item.kind !== "evidence" || item.event.evidence_key !== event.evidence_key) {
          return item;
        }
        found = true;
        return { ...item, event };
      });
      return found
        ? next
        : [...next, { id: newItemId(), kind: "evidence" as const, event }];
    });
  }

  const dispatchEvent = useCallback(
    (ev: ChatEvent, options: DispatchEventOptions = {}) => {
      const replay = options.replay === true;
      const updateStreamStatus = (status: string) => {
        effects.onStreamStatus(status, replay);
      };
      const updateConnectionState = (state: ChatConnectionState) => {
        if (!replay) effects.onConnectionState(state);
      };
      switch (ev.type) {
        case "delta":
          if (replay) return;
          effects.onAssistantDelta(ev.content);
          effects.onConnectionState("streaming");
          updateStreamStatus("Receiving response");
          return;
        case "final":
          if (replay) return;
          effects.onAssistantFinal(ev.content);
          effects.onConnectionState("streaming");
          updateStreamStatus("Finalizing response");
          return;
        case "tool_call":
          updateStreamStatus(`Running ${ev.tool}`);
          if (HIDDEN_TOOL_CHIPS.has(ev.tool)) return;
          pushItem({
            kind: "tool",
            id: ev.id,
            tool: ev.tool,
            args_preview: ev.args_preview,
            status: "running",
          });
          return;
        case "tool_result":
          updateStreamStatus(
            ev.ok ? `Finished ${ev.tool}` : `${ev.tool} failed: ${ev.summary}`,
          );
          if (HIDDEN_TOOL_CHIPS.has(ev.tool)) return;
          patchTool(ev.id, { status: ev.ok ? "ok" : "error", summary: ev.summary });
          return;
        case "dag_started":
          updateStreamStatus(`Planning ${ev.nodes.length} DAG nodes`);
          pushItem({
            kind: "dag",
            dag_run_id: ev.dag_run_id,
            goal: ev.goal,
            status: "running",
            nodes: ev.nodes.map((node) => ({
              node_id: node.id,
              agent: node.agent,
              skill: node.skill,
              deps: node.deps || [],
              args_preview: node.args || {},
              status: "pending",
            })),
          });
          return;
        case "dag_node_started":
          updateStreamStatus(`Running DAG node ${ev.node_id}`);
          patchDagNode(ev.dag_run_id, ev.node_id, {
            agent: ev.agent,
            skill: ev.skill,
            deps: ev.deps || [],
            args_preview: ev.args_preview || {},
            status: "running",
          });
          return;
        case "dag_node_complete":
          updateStreamStatus(
            ev.ok ? `Completed DAG node ${ev.node_id}` : `DAG node ${ev.node_id} failed`,
          );
          patchDagNode(ev.dag_run_id, ev.node_id, {
            agent: ev.agent,
            skill: ev.skill,
            status: ev.ok ? "complete" : "error",
            summary: ev.summary,
            error: ev.ok ? null : ev.summary,
            result: ev.result,
            grant_id: ev.grant_id,
            file_ops: ev.file_ops || [],
            elapsed_ms: ev.elapsed_ms,
          });
          return;
        case "dag_node_skipped":
          updateStreamStatus(`Skipped DAG node ${ev.node_id}`);
          patchDagNode(ev.dag_run_id, ev.node_id, {
            agent: ev.agent,
            skill: ev.skill,
            status: "skipped",
            summary: ev.summary,
            result: ev.result,
            grant_id: ev.grant_id,
            file_ops: ev.file_ops || [],
            elapsed_ms: ev.elapsed_ms,
          });
          return;
        case "dag_complete":
          updateStreamStatus(ev.ok ? "DAG complete" : "DAG failed");
          patchDag(ev.dag_run_id, {
            status: ev.ok ? "complete" : "error",
            summary: ev.summary,
          });
          return;
        case "review_loop_event": {
          const payload = ev.payload || {};
          const nextStep: ReviewLoopChatStep = {
            event_type: ev.event_type,
            status: ev.status,
            severity: ev.severity,
            message: ev.message,
            payload,
          };
          updateStreamStatus(`Reviewer loop ${ev.event_type}`);
          setActivityItems((m) => {
            const existing = m.find(
              (it) => it.kind === "review_loop" && it.job_id === ev.job_id,
            );
            const isCritical =
              ev.severity === "critical" ||
              Number(payload.critical_finding_count || 0) > 0;
            const isFix = ev.event_type === "fix_proposed";
            const isFrozen =
              ev.event_type === "promotion_frozen" || Boolean(payload.promotion_frozen);
            const nextStatus =
              ev.event_type === "loop_completed"
                ? "complete"
                : ev.event_type === "loop_failed"
                  ? "failed"
                  : ev.event_type === "loop_killed"
                    ? "killed"
                    : isFrozen || isCritical
                      ? "blocked"
                      : ev.status || "running";
            if (existing) {
              return m.map((it) =>
                it.kind === "review_loop" && it.job_id === ev.job_id
                  ? {
                      ...it,
                      status: nextStatus,
                      summary: ev.message || it.summary,
                      critical_findings:
                        it.critical_findings +
                        (isCritical && ev.event_type === "finding_emitted" ? 1 : 0),
                      proposed_fixes: it.proposed_fixes + (isFix ? 1 : 0),
                      promotion_frozen: it.promotion_frozen || isFrozen,
                      events: [...it.events, nextStep].slice(-8),
                    }
                  : it,
              );
            }
            return [
              ...m,
              {
                id: newItemId(),
                kind: "review_loop",
                job_id: ev.job_id,
                agent: ev.agent,
                status: nextStatus,
                summary: ev.message,
                critical_findings:
                  isCritical && ev.event_type === "finding_emitted" ? 1 : 0,
                proposed_fixes: isFix ? 1 : 0,
                promotion_frozen: isFrozen,
                events: [nextStep],
              },
            ];
          });
          return;
        }
        case "evidence_event":
          updateStreamStatus(ev.title || ev.event_type);
          upsertEvidenceEvent(ev);
          return;
        case "approval_required":
          updateStreamStatus(`Waiting for handoff approval to ${ev.handoff.to}`);
          pushItem({
            kind: "handoff",
            from: ev.handoff.from,
            to: ev.handoff.to,
            skill: ev.handoff.skill,
            grant_id: ev.handoff.grant_id,
            scopes: ev.handoff.scopes,
            args_preview: ev.handoff.args_preview,
            needs_approval: true,
            approval_id: ev.approval_id,
            status: "proposed",
            progress: [],
          });
          return;
        case "agent_handoff":
          updateStreamStatus(`Handing off to ${ev.to}.${ev.skill}`);
          setActivityItems((m) => {
            const existing = m.find(
              (it) => it.kind === "handoff" && it.grant_id === ev.grant_id,
            );
            if (existing) {
              return m.map((it) =>
                it.kind === "handoff" && it.grant_id === ev.grant_id
                  ? { ...it, status: "running" as const }
                  : it,
              );
            }
            return [
              ...m,
              {
                id: newItemId(),
                kind: "handoff",
                from: ev.from,
                to: ev.to,
                skill: ev.skill,
                grant_id: ev.grant_id,
                scopes: ev.scopes,
                args_preview: ev.args_preview,
                needs_approval: false,
                status: "running",
                progress: [],
              },
            ];
          });
          return;
        case "agent_setup_required":
          updateConnectionState("waiting");
          updateStreamStatus(`Setup required for ${ev.agent}`);
          pushItem({
            kind: "setup_required",
            agent: ev.agent,
            skill: ev.skill,
            setup: ev.setup,
            missing_required: ev.missing_required,
          });
          return;
        case "agent_progress": {
          const message =
            (ev.payload?.message as string | undefined) ??
            (ev.payload?.text as string | undefined) ??
            JSON.stringify(ev.payload);
          updateStreamStatus(message);
          setActivityItems((m) => {
            let changed = false;
            const next = m.map((it) => {
              if (it.kind !== "handoff" || it.grant_id !== ev.grant_id) return it;
              const progress = appendProgressEntry(it.progress, {
                kind: ev.kind,
                message,
              });
              if (progress === it.progress) return it;
              changed = true;
              return { ...it, progress };
            });
            return changed ? next : m;
          });
          return;
        }
        case "agent_question":
          updateConnectionState("waiting");
          updateStreamStatus("Waiting for your answer");
          pushItem({
            kind: "question",
            question_id: ev.question_id,
            grant_id: ev.grant_id,
            prompt: ev.prompt,
            status: "pending",
          });
          return;
        case "agent_question_answered":
          updateStreamStatus("Answer submitted");
          setActivityItems((m) =>
            m.map((it) =>
              it.kind === "question" && it.question_id === ev.question_id
                ? { ...it, status: "answered" as const, answer: ev.answer }
                : it,
            ),
          );
          return;
        case "agent_input_request":
          updateConnectionState("waiting");
          updateStreamStatus("Waiting for structured input");
          pushItem({
            kind: "input",
            request_id: ev.request_id,
            grant_id: ev.grant_id,
            title: ev.title,
            reason: ev.reason,
            schema: ev.schema,
            ui_schema: ev.ui_schema,
            status: "pending",
          });
          return;
        case "agent_input_submitted":
          updateStreamStatus(ev.ok ? "Input submitted" : "Input request timed out");
          setActivityItems((m) =>
            m.map((it) =>
              it.kind === "input" && it.request_id === ev.request_id
                ? {
                    ...it,
                    status: ev.ok ? ("submitted" as const) : ("timeout" as const),
                    value_preview: ev.value_preview,
                  }
                : it,
            ),
          );
          return;
        case "agent_input_timeout":
          updateStreamStatus("Input request timed out");
          setActivityItems((m) =>
            m.map((it) =>
              it.kind === "input" && it.request_id === ev.request_id
                ? { ...it, status: "timeout" as const }
                : it,
            ),
          );
          return;
        case "scope_request":
          updateStreamStatus(
            ev.decision === "auto_approve"
              ? "Auto-approving scope request"
              : "Waiting for scope approval",
          );
          pushItem({
            kind: "scope",
            request_id: ev.request_id,
            grant_id: ev.grant_id,
            reason: ev.reason,
            requested: ev.requested,
            original: ev.original_scopes,
            status: ev.decision === "auto_approve" ? "auto_approving" : "awaiting_user",
          });
          return;
        case "scope_approval_required":
          updateConnectionState("waiting");
          updateStreamStatus("Waiting for scope approval");
          patchScope(ev.request_id, {
            approval_id: ev.approval_id,
            policy_reason: ev.policy_reason,
            proposed: ev.proposed_grant,
            status: "awaiting_user",
          });
          return;
        case "scope_grant":
          updateStreamStatus("Scope expanded");
          patchScope(ev.request_id, {
            status: "approved",
            decided_by: ev.decided_by,
            granted: ev.scopes,
          });
          return;
        case "scope_denied":
          updateStreamStatus("Scope request denied");
          patchScope(ev.request_id, {
            status: "denied",
            decided_by: ev.decided_by,
            denial_reason: ev.reason,
          });
          return;
        case "handoff_complete":
          updateStreamStatus(ev.ok ? "Handoff complete" : "Handoff failed");
          patchHandoff(ev.grant_id, {
            status: ev.ok ? "complete" : "error",
            summary: ev.summary,
            file_ops: ev.file_ops,
          });
          return;
        case "handoff_denied":
          updateStreamStatus("Handoff denied");
          patchHandoff(ev.grant_id, { status: "denied" });
          return;
        case "error":
          if (replay) return;
          effects.onError(ev.message);
          effects.onConnectionState("error");
          return;
      }
    },
    [effects, pushItem],
  );

  return { messages, activityItems, items, loadHistory, pushItem, resetItems, dispatchEvent };
}

// ---------------------------------------------------------------------------
// Item renderer + activity cards (relocated verbatim from Chat.tsx; styling is
// owned by a later wave and intentionally preserved here).
// ---------------------------------------------------------------------------

export const ChatItemView = memo(function ChatItemView({
  item,
  streaming,
}: {
  item: Item;
  streaming: boolean;
}) {
  if (item.kind === "msg") return <ChatMessageRow msg={item} streaming={streaming} />;
  if (item.kind === "handoff") return <HandoffCard ev={item} />;
  if (item.kind === "dag") return <DagCard ev={item} />;
  if (item.kind === "review_loop") return <ReviewLoopCard ev={item} />;
  if (item.kind === "question") return <QuestionCard ev={item} />;
  if (item.kind === "input") return <ChatInputRequestCard ev={item} />;
  if (item.kind === "scope") return <ScopeCard ev={item} />;
  if (item.kind === "evidence") return <LiveEvidenceCard ev={item.event} />;
  if (item.kind === "setup_required") {
    return <ChatAgentSetupRequiredCard ev={item} />;
  }
  return <ChatToolChip ev={item} />;
});

function compactValue(value: unknown, max = 130): string {
  const raw = typeof value === "string" ? value : JSON.stringify(value);
  if (!raw) return "";
  return raw.length > max ? `${raw.slice(0, max)}…` : raw;
}

function DagCard({ ev }: { ev: DagEvent }) {
  const inspector = useOptionalWorkspaceInspector();
  const shown = ev;
  const isDone = shown.status === "complete";
  const isError = shown.status === "error";
  const active = shown.status === "running";
  const completed = shown.nodes.filter((node) => node.status === "complete").length;
  const errored = shown.nodes.filter((node) => node.status === "error").length;
  const skipped = shown.nodes.filter((node) => node.status === "skipped").length;
  const finished = completed + errored + skipped;
  const progressPct = shown.nodes.length
    ? Math.round((finished / shown.nodes.length) * 100)
    : 0;
  const dagError = isError ? shown.summary || "error" : null;
  const accent = isDone
    ? "from-signal-live/20 via-signal-live/12 to-ink-muted/10 border-signal-live/45"
    : isError
    ? "from-signal-danger/20 via-signal-danger/12 to-signal-danger/20 border-signal-danger/50"
    : "from-signal-authority/20 via-signal-authority/12 to-ink-muted/10 border-signal-authority/45";

  return (
    <div className="flex justify-start">
      <div
        className={`w-full max-w-[92%] rounded-2xl border bg-gradient-to-br p-[1px] ${accent}`}
      >
        <div className="rounded-2xl bg-runtime-bg/90 p-4">
          <div className="flex flex-wrap items-center gap-2 text-xs uppercase">
            <span
              className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 font-semibold ${
                isDone
                  ? "bg-signal-live/12 text-signal-live"
                  : isError
                  ? "bg-signal-danger/12 text-signal-danger"
                  : "bg-signal-authority/12 text-signal-authority"
              }`}
            >
              <span
                className={`h-1.5 w-1.5 rounded-full ${
                  active
                    ? "bg-signal-authority animate-pulse"
                    : isDone
                    ? "bg-signal-live"
                    : "bg-signal-danger"
                }`}
              />
              {isDone ? "DAG complete" : isError ? "DAG failed" : "DAG running"}
            </span>
            <span className="text-ink-muted normal-case">
              run <span className="font-mono">{shown.dag_run_id.slice(0, 12)}</span>
            </span>
            <span className="text-ink-muted normal-case">
              {completed}/{shown.nodes.length} complete
              {errored ? ` · ${errored} failed` : ""}
            </span>
          </div>

          <div className="mt-3 text-sm text-ink">{shown.goal}</div>
          {dagError && (
            <InlineAlert tone="red" role="alert" className="mt-2 text-xs">
              {dagError}
            </InlineAlert>
          )}
          {shown.summary && !dagError && (
            <div className="mt-2 text-xs text-ink-muted">{shown.summary}</div>
          )}

          <div className="mt-4 h-1.5 overflow-hidden rounded-full bg-runtime-raised">
            <div
              className={`h-full ${
                isError ? "bg-signal-danger" : isDone ? "bg-signal-live" : "bg-signal-authority"
              }`}
              style={{ width: `${progressPct}%` }}
            />
          </div>

          <div className="mt-4 divide-y divide-runtime-line-soft overflow-hidden rounded-md border border-runtime-line-soft/60 bg-runtime-panel/50">
            {shown.nodes.map((node) => (
              <DagNodeRow key={node.node_id} node={node} />
            ))}
          </div>

          <div className="mt-4">
            {inspector ? (
              <ToolbarButton onClick={() => inspector.openPanel("activity")}>
                Inspect in activity
              </ToolbarButton>
            ) : (
              <ToolbarLink href={runtimeControlReceiptHref(`dag/${ev.dag_run_id}`)}>
                Open run
              </ToolbarLink>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

function DagNodeRow({ node }: { node: DagNodeItem }) {
  const dot =
    node.status === "running"
      ? "bg-signal-authority animate-pulse"
      : node.status === "complete"
      ? "bg-signal-live"
      : node.status === "error"
      ? "bg-signal-danger"
      : node.status === "skipped"
      ? "bg-ink-muted"
      : "bg-ink-faint";
  const args = Object.entries(node.args_preview || {}).slice(0, 3);
  const evidenceCards = findChatEvidenceCards([node.result]);

  return (
    <div className="p-3">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs">
        <span className={`h-1.5 w-1.5 rounded-full ${dot}`} />
        <span className="font-mono text-ink">{node.node_id}</span>
        <span className="text-ink-muted">·</span>
        <span className="font-mono text-ink-soft">
          {node.agent}.{node.skill}
        </span>
        {node.elapsed_ms != null && (
          <span className="text-ink-faint">{node.elapsed_ms}ms</span>
        )}
        {node.grant_id && (
          <span className="text-ink-muted">
            grant <span className="font-mono">{node.grant_id.slice(0, 8)}</span>
          </span>
        )}
      </div>
      {node.deps.length > 0 && (
        <div className="mt-1 text-xs text-ink-muted">
          deps <span className="font-mono text-ink-dim">{node.deps.join(", ")}</span>
        </div>
      )}
      {args.length > 0 && (
        <div className="mt-2 space-y-0.5 font-mono text-[11px] text-ink-muted">
          {args.map(([key, value]) => (
            <div key={key} className="truncate">
              <span>{key}=</span>
              <span className="text-ink-soft">{compactValue(value)}</span>
            </div>
          ))}
        </div>
      )}
      {node.summary && (
        <div className="mt-2 text-xs text-ink-dim">
          <span className="text-ink-faint">
            {node.status === "error" ? "error " : "result "}
          </span>
          <span className="font-mono text-ink-soft">{node.summary}</span>
        </div>
      )}
      {node.error && node.error !== node.summary && (
        <InlineAlert tone="red" role="alert" className="mt-2 text-xs">
          {node.error}
        </InlineAlert>
      )}
      <ChatEvidenceCards cards={evidenceCards} compact />
      {node.file_ops && node.file_ops.length > 0 && (
        <ChatFileOpsList ops={node.file_ops} />
      )}
    </div>
  );
}

function ReviewLoopCard({ ev }: { ev: ReviewLoopChatEvent }) {
  const isBlocked = ev.status === "blocked" || ev.promotion_frozen;
  const isDone = ev.status === "complete";
  const isFailed = ["failed", "killed", "error"].includes(ev.status);
  const accent = isBlocked
    ? "border-signal-authority/45 bg-signal-authority/12"
    : isFailed
      ? "border-signal-danger/50 bg-signal-danger/12"
      : isDone
      ? "border-signal-live/45 bg-signal-live/12"
        : "border-signal-authority/45 bg-signal-authority/12";
  const latest = ev.events[ev.events.length - 1];
  const evidenceCards = findChatEvidenceCards([
    latest?.payload,
    ...ev.events.map((event) => event.payload),
  ]);
  return (
    <div className="flex justify-start">
      <div className={`w-full max-w-[92%] rounded-2xl border p-4 ${accent}`}>
        <div className="flex flex-wrap items-center gap-2 text-xs uppercase">
          <span
            className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 font-semibold ${
              isBlocked
                ? "bg-signal-authority/12 text-signal-authority"
                : isFailed
                  ? "bg-signal-danger/12 text-signal-danger"
                  : isDone
                    ? "bg-signal-live/12 text-signal-live"
                    : "bg-signal-authority/12 text-signal-authority"
            }`}
          >
            <span
              className={`h-1.5 w-1.5 rounded-full ${
                isDone
                  ? "bg-signal-live"
                  : isFailed
                    ? "bg-signal-danger"
                    : isBlocked
                      ? "bg-signal-authority"
                      : "bg-signal-authority animate-pulse"
              }`}
            />
            Reviewer loop {ev.status}
          </span>
          <span className="text-ink-muted normal-case">{ev.agent}</span>
          <span className="font-mono text-ink-muted normal-case">
            {ev.job_id.slice(0, 16)}
          </span>
        </div>

        <div className="mt-3 grid gap-2 md:grid-cols-[1fr_24px_1fr_24px_1fr]">
          <ReviewLoopNode label="reviewer" value={ev.agent} tone="cyan" />
          <ReviewLoopEdge active />
          <ReviewLoopNode
            label="findings"
            value={`${ev.critical_findings} critical`}
            detail={latest?.event_type || "waiting"}
            tone={ev.critical_findings > 0 ? "amber" : "neutral"}
          />
          <ReviewLoopEdge active={ev.proposed_fixes > 0 || ev.promotion_frozen} />
          <ReviewLoopNode
            label="promotion gate"
            value={ev.promotion_frozen ? "frozen" : "open"}
            detail={`${ev.proposed_fixes} fix refs`}
            tone={ev.promotion_frozen ? "amber" : "emerald"}
          />
        </div>

        {ev.summary && <div className="mt-3 text-sm text-ink-soft">{ev.summary}</div>}

        <div className="mt-3 grid gap-1.5">
          {ev.events.slice(-5).map((event, index) => (
            <div
              key={`${event.event_type}:${index}`}
              className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-runtime-line-soft/80 bg-runtime-bg/50 px-2 py-1.5 text-[11px]"
            >
              <span className="font-mono text-ink-soft">{event.event_type}</span>
              <span className="text-ink-muted">
                {event.message || event.status || event.severity || "-"}
              </span>
            </div>
          ))}
        </div>
        <ChatEvidenceCards cards={evidenceCards} />
      </div>
    </div>
  );
}

function reviewLoopToneToMetricTone(tone: "cyan" | "amber" | "emerald" | "neutral") {
  return tone === "cyan" ? "neutral" : tone;
}

function ReviewLoopNode({
  label,
  value,
  detail,
  tone,
}: {
  label: string;
  value: string;
  detail?: string;
  tone: "cyan" | "amber" | "emerald" | "neutral";
}) {
  return (
    <SummaryMetric
      label={label}
      value={value}
      detail={detail}
      tone={reviewLoopToneToMetricTone(tone)}
      size="compact"
      className="px-3 py-2"
    />
  );
}

function ReviewLoopEdge({ active }: { active: boolean }) {
  return (
    <div className="hidden items-center justify-center md:flex">
      <div className={`h-px w-full ${active ? "bg-ink-muted" : "bg-runtime-raised"}`} />
    </div>
  );
}

function HandoffCard({ ev }: { ev: HandoffEvent }) {
  const inspector = useOptionalWorkspaceInspector();
  const [busy, setBusy] = useState(false);
  const [decideErr, setDecideErr] = useState<string | null>(null);
  const [runErr, setRunErr] = useState<string | null>(null);
  const [rerunBusy, setRerunBusy] = useState(false);
  const [rerunNote, setRerunNote] = useState<string | null>(null);
  const [rerunGrantId, setRerunGrantId] = useState<string | null>(null);
  const isPending =
    ev.status === "proposed" && ev.needs_approval && Boolean(ev.approval_id);
  const isRunning = ev.status === "running";
  const isDone = ev.status === "complete";
  const isDenied = ev.status === "denied";
  const isError = ev.status === "error";

  async function decide(decision: "approve" | "deny") {
    if (!ev.approval_id || busy) return;
    setBusy(true);
    setDecideErr(null);
    try {
      await approveHandoff(ev.approval_id, decision);
    } catch (ex) {
      setDecideErr(ex instanceof Error ? ex.message : String(ex));
    } finally {
      setBusy(false);
    }
  }

  async function rerun() {
    setRerunBusy(true);
    setRunErr(null);
    setRerunNote(null);
    setRerunGrantId(null);
    try {
      const next = await rerunSubagentRun(ev.grant_id);
      setRerunGrantId(next.grant_id);
      setRerunNote(`rerun ${next.grant_id.slice(0, 12)} returned ${next.status}`);
    } catch (ex) {
      setRunErr(ex instanceof Error ? ex.message : String(ex));
    } finally {
      setRerunBusy(false);
    }
  }

  const accent = isDone
    ? "from-signal-live/20 via-signal-live/12 to-ink-muted/10 border-signal-live/45"
    : isDenied
    ? "from-runtime-line/20 via-runtime-line/10 to-runtime-line/20 border-runtime-line"
    : isError
    ? "from-signal-danger/20 via-signal-danger/12 to-signal-danger/20 border-signal-danger/50"
    : "from-signal-authority/20 via-signal-authority/12 to-ink-muted/10 border-signal-authority/45";

  return (
    <div className="flex min-w-0 justify-start">
      <div
        className={`w-full max-w-[92%] overflow-hidden rounded-2xl border bg-gradient-to-br p-[1px] ${accent}`}
      >
        <div className="min-w-0 overflow-hidden rounded-2xl bg-runtime-bg/90 p-4">
          <div className="flex flex-wrap items-center gap-2 text-xs uppercase">
            <span
              className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 font-semibold ${
                isDenied
                  ? "bg-runtime-raised text-ink-dim"
                  : isDone
                  ? "bg-signal-live/12 text-signal-live"
                  : isError
                  ? "bg-signal-danger/12 text-signal-danger"
                  : "bg-signal-authority/12 text-signal-authority"
              }`}
            >
              <span
                className={`h-1.5 w-1.5 rounded-full ${
                  isRunning
                    ? "bg-signal-authority animate-pulse"
                    : isDone
                    ? "bg-signal-live"
                    : isDenied
                    ? "bg-ink-muted"
                    : isError
                    ? "bg-signal-danger"
                    : "bg-signal-authority"
                }`}
              />
              {isPending
                ? "Handoff requested"
                : isRunning
                ? "Agents collaborating"
                : isDone
                ? "Handoff complete"
                : isDenied
                ? "Handoff denied"
                : isError
                ? "Handoff failed"
                : "Handoff"}
            </span>
            <span className="break-anywhere text-ink-muted normal-case">
              grant <span className="font-mono">{ev.grant_id.slice(0, 8)}</span>
            </span>
          </div>

          <div className="mt-3 flex min-w-0 flex-wrap items-center gap-2 font-mono text-sm">
            <span className="break-anywhere rounded-md bg-runtime-raised/80 px-2 py-1 text-ink-soft">
              {ev.from}
            </span>
            <Arrow running={isRunning || isPending} />
            <span className="break-anywhere rounded-md bg-runtime-raised/80 px-2 py-1 text-ink-soft">
              {ev.to}
            </span>
            <span className="text-ink-muted">·</span>
            <span className="break-anywhere text-ink-soft">{ev.skill}</span>
          </div>

          <div className="mt-3 grid grid-cols-1 gap-x-6 gap-y-1 text-xs text-ink-dim sm:grid-cols-2">
            <div className="min-w-0">
              <span className="text-ink-muted">bucket </span>
              <span className="break-anywhere font-mono text-ink-soft">
                {ev.scopes.bucket}
              </span>
            </div>
            <div className="min-w-0">
              <span className="text-ink-muted">mode </span>
              <span className="break-anywhere font-mono text-ink-soft">
                {ev.scopes.mode}
              </span>
            </div>
            <div className="min-w-0">
              <span className="text-ink-muted">writes </span>
              <span className="break-anywhere font-mono text-ink-soft">
                {writeScopeLabels(ev.scopes).join(", ") || "(none)"}
              </span>
            </div>
            <div className="min-w-0">
              <span className="text-ink-muted">expires </span>
              <span className="font-mono text-ink-soft">{ev.scopes.ttl_seconds}s</span>
            </div>
            {ev.scopes.allow_patterns?.length > 0 && (
              <div className="min-w-0 sm:col-span-2">
                <span className="text-ink-muted">read </span>
                <span className="break-anywhere font-mono text-ink-soft">
                  {ev.scopes.allow_patterns.join(", ")}
                </span>
              </div>
            )}
            {ev.scopes.deny_patterns?.length > 0 && (
              <div className="min-w-0 sm:col-span-2">
                <span className="text-ink-muted">deny </span>
                <span className="break-anywhere font-mono text-signal-danger">
                  {ev.scopes.deny_patterns.join(", ")}
                </span>
              </div>
            )}
          </div>

          {Object.keys(ev.args_preview || {}).length > 0 && (
            <div className="mt-3 min-w-0 overflow-hidden rounded-md border border-runtime-line-soft/60 bg-runtime-panel/60 p-2 font-mono text-xs text-ink-dim">
              {Object.entries(ev.args_preview).map(([k, v]) => (
                <div key={k} className="break-anywhere whitespace-pre-wrap">
                  <span className="text-ink-muted">{k}=</span>
                  <span className="text-ink-soft">
                    {typeof v === "string" ? v : JSON.stringify(v)}
                  </span>
                </div>
              ))}
            </div>
          )}

          {ev.progress.length > 0 && (
            <ProgressTrail entries={ev.progress} live={isRunning} />
          )}

          {ev.summary && !isPending && (
            <div className="mt-3 text-xs text-ink-dim">
              <span className="text-ink-muted">{isError ? "error " : "result "}</span>
              <span
                className={`break-anywhere font-mono ${isError ? "text-signal-danger" : "text-ink-soft"}`}
              >
                {ev.summary}
              </span>
            </div>
          )}

          {ev.file_ops && ev.file_ops.length > 0 && (
            <ChatFileOpsList ops={ev.file_ops} />
          )}

          <div className="mt-4 flex flex-wrap gap-2">
            {inspector ? (
              <ToolbarButton onClick={() => inspector.openPanel("activity")}>
                Inspect response
              </ToolbarButton>
            ) : (
              <ToolbarLink href={runtimeControlReceiptHref(`subagent/${ev.grant_id}`)}>
                View response
              </ToolbarLink>
            )}
            {(isDone || isError) && (
              <ToolbarButton onClick={rerun} disabled={rerunBusy}>
                {rerunBusy ? "rerunning..." : "rerun"}
              </ToolbarButton>
            )}
          </div>

          {(runErr || rerunNote) && (
            <div
              className={`mt-3 rounded-md border px-3 py-2 text-xs ${
                runErr
                  ? "border-signal-danger/50 bg-signal-danger/12 text-signal-danger"
                  : "border-signal-live/45 bg-signal-live/12 text-signal-live"
              }`}
            >
              {runErr || rerunNote}
              {rerunGrantId && !runErr && (
                <div className="mt-2">
                  {inspector ? (
                    <ToolbarButton
                      onClick={() => inspector.openPanel("activity")}
                      size="xs"
                    >
                      Inspect rerun
                    </ToolbarButton>
                  ) : (
                    <ToolbarLink
                      href={runtimeControlReceiptHref(`subagent/${rerunGrantId}`)}
                      size="xs"
                    >
                      Open rerun
                    </ToolbarLink>
                  )}
                </div>
              )}
            </div>
          )}

          {isPending && (
            <div className="mt-4 flex flex-wrap items-center gap-2">
              <ToolbarButton
                disabled={busy}
                onClick={() => decide("approve")}
                variant="success"
              >
                {busy ? "sending..." : "approve"}
              </ToolbarButton>
              <ToolbarButton disabled={busy} onClick={() => decide("deny")}>
                deny
              </ToolbarButton>
              <span className="text-xs text-ink-muted">Auto-denies after 120s.</span>
              {decideErr && (
                <span className="basis-full text-xs text-signal-danger">
                  failed: {decideErr}
                </span>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function ProgressTrail({ entries, live }: { entries: ProgressEntry[]; live: boolean }) {
  return (
    <div className="mt-3 min-w-0 space-y-1 overflow-hidden rounded-md border border-signal-authority/45 bg-signal-authority/12 p-2 text-xs">
      {entries.map((e, i) => {
        const isLast = i === entries.length - 1;
        return (
          <div key={i} className="flex min-w-0 items-start gap-2">
            <span
              className={`mt-1 h-1.5 w-1.5 shrink-0 rounded-full ${
                live && isLast ? "bg-signal-authority animate-pulse" : "bg-signal-authority/60"
              }`}
            />
            <span className="shrink-0 text-ink-muted">[{e.kind}]</span>
            <span className="min-w-0 flex-1 break-anywhere whitespace-pre-wrap text-ink-soft">
              {e.message}
            </span>
          </div>
        );
      })}
    </div>
  );
}

function Arrow({ running }: { running: boolean }) {
  return (
    <span
      className={`inline-flex items-center ${running ? "animate-pulse text-signal-authority" : "text-ink-muted"}`}
      aria-hidden
    >
      <Icon name="arrow-right" size={14} strokeWidth={1.75} />
    </span>
  );
}
