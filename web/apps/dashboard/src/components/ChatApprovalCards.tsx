import { useState } from "react";
import {
  answerQuestion,
  approveScopeRequest,
  type ScopeOriginal,
  type ScopeProposed,
  type ScopeRequested,
} from "../api";
import {
  ChatEventActions,
  ChatEventCard,
  ChatEventHeader,
  ChatEventMeta,
  ChatEventStatus,
  type ChatEventTone,
} from "./ChatEventCard";
import { writeScopeLabels } from "./chatScopeUtils";
import { Icon } from "./Icon";

export type QuestionEvent = {
  kind: "question";
  question_id: string;
  grant_id: string;
  prompt: string;
  status: "pending" | "answered";
  answer?: string;
};

export type ScopeEvent = {
  kind: "scope";
  request_id: string;
  grant_id: string;
  reason: string;
  requested: ScopeRequested;
  original: ScopeOriginal;
  proposed?: ScopeProposed;
  approval_id?: string;
  policy_reason?: string;
  status: "auto_approving" | "awaiting_user" | "approved" | "denied";
  decided_by?: string;
  granted?: ScopeProposed;
  denial_reason?: string;
};

export function ScopeCard({ ev }: { ev: ScopeEvent }) {
  const [busy, setBusy] = useState(false);
  const [decideErr, setDecideErr] = useState<string | null>(null);
  const isPending =
    ev.status === "awaiting_user" && Boolean(ev.approval_id);
  const isApproved = ev.status === "approved";
  const isDenied = ev.status === "denied";
  const isAuto = ev.status === "auto_approving" && !isApproved && !isDenied;

  async function decide(decision: "approve" | "deny") {
    if (!ev.approval_id || busy) return;
    setBusy(true);
    setDecideErr(null);
    try {
      await approveScopeRequest(ev.approval_id, decision);
    } catch (ex) {
      setDecideErr(ex instanceof Error ? ex.message : String(ex));
    } finally {
      setBusy(false);
    }
  }

  const tone: ChatEventTone = isApproved ? "emerald" : isDenied ? "red" : "amber";
  const statusLabel = isPending
    ? "Subagent asks for more access"
    : isAuto
      ? "Auto-approving scope expansion"
      : isApproved
        ? "Scope expanded"
        : "Scope denied";

  const newReads = ev.requested.read_patterns.filter(
    (p) => !ev.original.allow_patterns.includes(p),
  );
  const existingWrites = writeScopeLabels(ev.original);
  const newWrites = writeScopeLabels(ev.requested).filter(
    (p) => !existingWrites.includes(p),
  );

  return (
    <ChatEventCard tone={tone}>
      <ChatEventHeader>
        <ChatEventStatus
          tone={tone}
          label={statusLabel}
          pulse={isPending || isAuto}
        />
        <ChatEventMeta>
          req <span className="font-mono">{ev.request_id.slice(0, 10)}</span>
        </ChatEventMeta>
        {ev.decided_by && (
          <ChatEventMeta>
            by <span className="font-mono">{ev.decided_by}</span>
          </ChatEventMeta>
        )}
      </ChatEventHeader>

      <div className="break-anywhere mt-3 text-sm text-ink">{ev.reason}</div>

      <div className="mt-3 grid grid-cols-1 gap-x-6 gap-y-1 text-xs text-ink-dim sm:grid-cols-2">
        {newReads.length > 0 && (
          <div className="min-w-0 sm:col-span-2">
            <span className="text-ink-muted">+ read </span>
            <span className="break-anywhere font-mono text-signal-live">
              {newReads.join(", ")}
            </span>
          </div>
        )}
        {newWrites.length > 0 && (
          <div className="min-w-0 sm:col-span-2">
            <span className="text-ink-muted">+ write </span>
            <span className="break-anywhere font-mono text-signal-live">
              {newWrites.join(", ")}
            </span>
          </div>
        )}
        <div className="min-w-0">
          <span className="text-ink-muted">mode </span>
          <span className="break-anywhere font-mono text-ink-soft">
            {ev.requested.mode}
          </span>
        </div>
        <div className="min-w-0">
          <span className="text-ink-muted">ttl </span>
          <span className="font-mono text-ink-soft">
            {ev.requested.ttl_seconds}s
          </span>
        </div>
      </div>

      {ev.policy_reason && isPending && (
        <div className="break-anywhere mt-2 text-xs text-ink-muted">
          policy: {ev.policy_reason}
        </div>
      )}

      {isApproved && ev.granted && (
        <div className="mt-3 text-xs text-ink-dim">
          <span className="text-ink-muted">granted </span>
          <span className="break-anywhere font-mono text-ink-soft">
            {ev.granted.allow_patterns.join(", ") || "(no read patterns)"}
          </span>
          <span className="text-ink-muted"> / {ev.granted.ttl_seconds}s</span>
        </div>
      )}

      {isDenied && ev.denial_reason && (
        <div className="break-anywhere mt-3 text-xs text-signal-danger">
          <span className="text-ink-muted">denied: </span>
          {ev.denial_reason}
        </div>
      )}

      {isPending && (
        <ChatEventActions>
          <button
            type="button"
            disabled={busy}
            onClick={() => decide("approve")}
            className="rounded-md bg-signal-live px-3 py-1.5 text-xs font-semibold text-runtime-bg hover:bg-signal-live/90 disabled:opacity-40"
          >
            {busy ? "sending..." : "grant"}
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={() => decide("deny")}
            className="rounded-md border border-runtime-line px-3 py-1.5 text-xs text-ink-soft hover:border-runtime-line-mid disabled:opacity-40"
          >
            deny
          </button>
          <span className="text-xs text-ink-muted">Auto-denies after 60s.</span>
          {decideErr && (
            <span className="basis-full text-xs text-signal-danger">
              failed: {decideErr}
            </span>
          )}
        </ChatEventActions>
      )}
    </ChatEventCard>
  );
}

export function QuestionCard({ ev }: { ev: QuestionEvent }) {
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [submitErr, setSubmitErr] = useState<string | null>(null);
  const isPending = ev.status === "pending";

  async function submit() {
    if (!draft.trim() || busy) return;
    setBusy(true);
    setSubmitErr(null);
    try {
      await answerQuestion(ev.question_id, draft.trim());
    } catch (ex) {
      setSubmitErr(ex instanceof Error ? ex.message : String(ex));
    } finally {
      setBusy(false);
    }
  }

  const tone: ChatEventTone = isPending ? "amber" : "emerald";

  return (
    <ChatEventCard tone={tone}>
      <ChatEventHeader>
        <ChatEventStatus
          tone={tone}
          label={isPending ? "Agent asks" : "Answered"}
          pulse={isPending}
        />
        <ChatEventMeta>
          q <span className="font-mono">{ev.question_id.slice(0, 10)}</span>
        </ChatEventMeta>
      </ChatEventHeader>
      <div className="break-anywhere mt-3 text-sm text-ink">{ev.prompt}</div>
      {isPending ? (
        <>
          <div className="mt-3 flex items-end gap-2 rounded-md border border-runtime-line-soft/60 bg-runtime-panel px-3 py-2">
            <textarea
              rows={1}
              value={draft}
              disabled={busy}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  submit();
                }
              }}
              placeholder="reply..."
              className="min-w-0 flex-1 resize-none bg-transparent text-sm text-ink placeholder:text-ink-faint outline-none"
            />
            <button
              type="button"
              onClick={submit}
              disabled={!draft.trim() || busy}
              className="shrink-0 rounded-md bg-signal-authority px-3 py-1 text-xs font-semibold text-runtime-bg hover:bg-signal-authority/90 disabled:opacity-40"
            >
              send
            </button>
          </div>
          {submitErr && (
            <div className="mt-2 text-xs text-signal-danger">
              failed: {submitErr}
            </div>
          )}
        </>
      ) : (
        <div className="mt-3 flex items-start gap-2 rounded-md border border-runtime-line-soft/60 bg-runtime-panel/60 p-2 text-sm text-ink-soft">
          <span
            className="mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-md border border-runtime-line-soft/70 bg-runtime-bg text-ink-muted"
            aria-hidden="true"
          >
            <Icon name="arrow-right" size={11} />
          </span>
          <span className="min-w-0 break-words">{ev.answer}</span>
        </div>
      )}
    </ChatEventCard>
  );
}
