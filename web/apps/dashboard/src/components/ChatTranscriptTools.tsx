import { useEffect, useMemo, useRef, useState } from "react";
import { ToolbarButton, copyTextToClipboard } from "./DashboardChrome";

type ChatTranscriptMessageLike = {
  role?: string | null;
  kind?: string | null;
  content?: string | null;
  markdown?: string | null;
  text?: string | null;
  createdAt?: string | null;
  created_at?: string | null;
  timestamp?: string | null;
  [key: string]: unknown;
};

type ClearLocalDraftCallback = () => Promise<void> | void;

export type ChatTranscriptToolsProps = {
  items?: readonly ChatTranscriptMessageLike[];
  messages?: readonly ChatTranscriptMessageLike[];
  title?: string;
  filename?: string;
  disabled?: boolean;
  className?: string;
  onClearDraft?: ClearLocalDraftCallback;
  clearLocalDraftCallbacks?: readonly ClearLocalDraftCallback[];
};

type TranscriptMessage = {
  role: string;
  content: string;
  timestamp: string | null;
};

type ToolAction = "copy-transcript" | "copy-answer" | "export-md" | "clear-draft";

type ToolStatus = {
  action: ToolAction;
  label: string;
};

const STATUS_RESET_MS = 1400;

export function ChatTranscriptTools({
  items,
  messages,
  title = "Chat transcript",
  filename,
  disabled = false,
  className,
  onClearDraft,
  clearLocalDraftCallbacks,
}: ChatTranscriptToolsProps) {
  const [status, setStatus] = useState<ToolStatus | null>(null);
  const resetTimerRef = useRef<number | null>(null);

  useEffect(() => {
    return () => {
      if (resetTimerRef.current !== null) {
        window.clearTimeout(resetTimerRef.current);
      }
    };
  }, []);

  const transcriptMessages = useMemo(
    () => normalizeMessages(messages ?? items ?? []),
    [items, messages],
  );
  const transcriptMarkdown = useMemo(
    () => formatTranscriptMarkdown(transcriptMessages, title),
    [title, transcriptMessages],
  );
  const latestAnswer = useMemo(
    () => latestAssistantAnswer(transcriptMessages),
    [transcriptMessages],
  );
  const draftCallbacks = useMemo(() => {
    const callbacks: ClearLocalDraftCallback[] = [];
    if (onClearDraft) callbacks.push(onClearDraft);
    if (clearLocalDraftCallbacks) callbacks.push(...clearLocalDraftCallbacks);
    return callbacks;
  }, [clearLocalDraftCallbacks, onClearDraft]);

  const hasTranscript = transcriptMessages.length > 0;
  const hasLatestAnswer = latestAnswer.length > 0;
  const canClearDraft = draftCallbacks.length > 0;
  const transcriptDisabled = disabled || !hasTranscript;
  const answerDisabled = disabled || !hasLatestAnswer;
  const clearDraftDisabled = disabled || !canClearDraft;

  function flash(action: ToolAction, label: string) {
    setStatus({ action, label });
    if (resetTimerRef.current !== null) {
      window.clearTimeout(resetTimerRef.current);
    }
    resetTimerRef.current = window.setTimeout(() => setStatus(null), STATUS_RESET_MS);
  }

  async function copyTranscript() {
    if (transcriptDisabled) return;
    try {
      await copyTextToClipboard(transcriptMarkdown);
      flash("copy-transcript", "copied");
    } catch {
      flash("copy-transcript", "copy failed");
    }
  }

  async function copyLatestAnswer() {
    if (answerDisabled) return;
    try {
      await copyTextToClipboard(latestAnswer);
      flash("copy-answer", "copied");
    } catch {
      flash("copy-answer", "copy failed");
    }
  }

  function exportMarkdown() {
    if (transcriptDisabled) return;
    try {
      downloadMarkdown(transcriptMarkdown, markdownFilename(filename ?? title));
      flash("export-md", "exported");
    } catch {
      flash("export-md", "export failed");
    }
  }

  async function clearDraft() {
    if (clearDraftDisabled) return;
    try {
      for (const callback of draftCallbacks) {
        await callback();
      }
      flash("clear-draft", "cleared");
    } catch {
      flash("clear-draft", "clear failed");
    }
  }

  return (
    <div
      className={cx("flex flex-wrap items-center gap-1.5", className)}
      aria-label="Transcript tools"
    >
      <TranscriptToolButton
        disabled={transcriptDisabled}
        onClick={copyTranscript}
        title="Copy full transcript"
      >
        {buttonLabel(status, "copy-transcript", "copy transcript")}
      </TranscriptToolButton>
      <TranscriptToolButton
        disabled={answerDisabled}
        onClick={copyLatestAnswer}
        title="Copy latest assistant answer"
      >
        {buttonLabel(status, "copy-answer", "copy answer")}
      </TranscriptToolButton>
      <TranscriptToolButton
        disabled={transcriptDisabled}
        onClick={exportMarkdown}
        title="Export transcript as Markdown"
      >
        {buttonLabel(status, "export-md", "export .md")}
      </TranscriptToolButton>
      <TranscriptToolButton
        disabled={clearDraftDisabled}
        onClick={clearDraft}
        title="Clear local draft"
      >
        {buttonLabel(status, "clear-draft", "clear draft")}
      </TranscriptToolButton>
      <span className="sr-only" role="status" aria-live="polite">
        {status?.label ?? ""}
      </span>
    </div>
  );
}

function TranscriptToolButton({
  children,
  disabled,
  onClick,
  title,
}: {
  children: string;
  disabled: boolean;
  onClick: () => Promise<void> | void;
  title: string;
}) {
  return (
    <ToolbarButton
      disabled={disabled}
      onClick={onClick}
      title={title}
      size="md"
    >
      {children}
    </ToolbarButton>
  );
}

function normalizeMessages(
  source: readonly ChatTranscriptMessageLike[],
): TranscriptMessage[] {
  return source.flatMap((item) => {
    const card = formatTimelineCard(item);
    if (card) return [card];

    const content = firstText(item.content, item.markdown, item.text);
    if (!content) return [];
    const role = firstText(item.role, item.kind) ?? "message";

    return [
      {
        role,
        content: content.trim(),
        timestamp: firstText(item.createdAt, item.created_at, item.timestamp),
      },
    ];
  });
}

function formatTimelineCard(item: ChatTranscriptMessageLike): TranscriptMessage | null {
  const kind = firstText(item.kind)?.trim().toLowerCase();
  if (!kind || kind === "msg" || kind === "message") return null;

  const content =
    kind === "handoff" ? formatHandoffCard(item)
    : kind === "scope" ? formatScopeCard(item)
    : kind === "dag" ? formatDagCard(item)
    : kind === "tool" ? formatToolCard(item)
    : kind === "question" ? formatQuestionCard(item)
    : kind === "input" ? formatInputCard(item)
    : kind === "evidence" ? formatEvidenceCard(item)
    : kind === "setup_required" ? formatSetupRequiredCard(item)
    : kind === "review_loop" ? formatReviewLoopCard(item)
    : null;

  if (!content) return null;
  return {
    role: cardRoleLabel(kind),
    content,
    timestamp: firstText(item.createdAt, item.created_at, item.timestamp),
  };
}

function formatHandoffCard(item: ChatTranscriptMessageLike): string {
  const lines: string[] = [];
  const scopes = objectValue(item.scopes);

  pushField(lines, "Status", textValue(item.status));
  pushField(lines, "Grant", textValue(item.grant_id));
  pushField(lines, "Route", routeLabel(textValue(item.from), textValue(item.to)));
  pushField(lines, "Tool", textValue(item.skill));
  pushField(lines, "Approval", truthy(item.needs_approval) ? textValue(item.approval_id) || "required" : "not required");

  if (scopes) {
    pushField(lines, "Bucket", textValue(scopes.bucket));
    pushField(lines, "Mode", textValue(scopes.mode));
    pushField(lines, "TTL", suffix(textValue(scopes.ttl_seconds), "s"));
    pushField(lines, "Read", listLabel(stringArray(scopes.allow_patterns), "(none)"));
    pushField(lines, "Deny", listLabel(stringArray(scopes.deny_patterns), "(none)"));
    pushField(lines, "Writes", listLabel(writeScopeLabels(scopes), "(none)"));
  }

  pushField(lines, "Summary", textValue(item.summary));
  pushJsonBlock(lines, "Arguments", objectValue(item.args_preview));
  pushProgress(lines, arrayValue(item.progress));
  pushFileOps(lines, arrayValue(item.file_ops));
  return lines.join("\n");
}

function formatScopeCard(item: ChatTranscriptMessageLike): string {
  const lines: string[] = [];
  const requested = objectValue(item.requested);
  const original = objectValue(item.original);
  const proposed = objectValue(item.proposed);
  const granted = objectValue(item.granted);
  const newReads = requested && original
    ? stringArray(requested.read_patterns).filter((p) => !stringArray(original.allow_patterns).includes(p))
    : [];
  const newWrites = requested && original
    ? writeScopeLabels(requested).filter((p) => !writeScopeLabels(original).includes(p))
    : [];

  pushField(lines, "Status", textValue(item.status));
  pushField(lines, "Request", textValue(item.request_id));
  pushField(lines, "Grant", textValue(item.grant_id));
  pushField(lines, "Approval", textValue(item.approval_id));
  pushField(lines, "Reason", textValue(item.reason));
  pushField(lines, "Policy", textValue(item.policy_reason));
  if (requested) {
    pushField(lines, "New read access", listLabel(newReads, "(none)"));
    pushField(lines, "New write access", listLabel(newWrites, "(none)"));
    pushField(lines, "Requested mode", textValue(requested.mode));
    pushField(lines, "Requested TTL", suffix(textValue(requested.ttl_seconds), "s"));
  }
  if (proposed) {
    pushField(lines, "Proposed read", listLabel(stringArray(proposed.allow_patterns), "(none)"));
    pushField(lines, "Proposed write", listLabel(writeScopeLabels(proposed), "(none)"));
  }
  if (granted) {
    pushField(lines, "Granted read", listLabel(stringArray(granted.allow_patterns), "(none)"));
    pushField(lines, "Granted write", listLabel(writeScopeLabels(granted), "(none)"));
    pushField(lines, "Granted TTL", suffix(textValue(granted.ttl_seconds), "s"));
  }
  pushField(lines, "Decided by", textValue(item.decided_by));
  pushField(lines, "Denial reason", textValue(item.denial_reason));
  return lines.join("\n");
}

function formatDagCard(item: ChatTranscriptMessageLike): string {
  const lines: string[] = [];
  pushField(lines, "Status", textValue(item.status));
  pushField(lines, "DAG run", textValue(item.dag_run_id));
  pushField(lines, "Goal", textValue(item.goal));
  pushField(lines, "Summary", textValue(item.summary));

  const nodes = arrayValue(item.nodes).map(objectValue).filter((node): node is Record<string, unknown> => Boolean(node));
  if (nodes.length > 0) {
    lines.push("", "### Nodes");
    for (const node of nodes) {
      const heading = [
        textValue(node.node_id) || "node",
        textValue(node.agent),
        textValue(node.skill),
      ].filter(Boolean).join(" - ");
      const status = textValue(node.status);
      const grant = textValue(node.grant_id);
      const summary = textValue(node.summary) || textValue(node.error);
      lines.push(`- **${heading}**${status ? ` (${status})` : ""}${grant ? ` - grant ${grant}` : ""}${summary ? ` - ${summary}` : ""}`);
      pushNestedFileOps(lines, arrayValue(node.file_ops));
    }
  }

  return lines.join("\n");
}

function formatToolCard(item: ChatTranscriptMessageLike): string {
  const lines: string[] = [];
  pushField(lines, "Tool", textValue(item.tool));
  pushField(lines, "Status", textValue(item.status));
  pushField(lines, "Summary", textValue(item.summary));
  pushJsonBlock(lines, "Arguments", objectValue(item.args_preview));
  return lines.join("\n");
}

function formatQuestionCard(item: ChatTranscriptMessageLike): string {
  const lines: string[] = [];
  pushField(lines, "Status", textValue(item.status));
  pushField(lines, "Question", textValue(item.question_id));
  pushField(lines, "Grant", textValue(item.grant_id));
  pushField(lines, "Prompt", textValue(item.prompt));
  pushField(lines, "Answer", textValue(item.answer));
  return lines.join("\n");
}

function formatInputCard(item: ChatTranscriptMessageLike): string {
  const lines: string[] = [];
  pushField(lines, "Status", textValue(item.status));
  pushField(lines, "Request", textValue(item.request_id));
  pushField(lines, "Grant", textValue(item.grant_id));
  pushField(lines, "Title", textValue(item.title));
  pushField(lines, "Reason", textValue(item.reason));
  pushJsonBlock(lines, "Submitted value", objectValue(item.value_preview));
  return lines.join("\n");
}

function formatEvidenceCard(item: ChatTranscriptMessageLike): string {
  const event = objectValue(item.event);
  if (!event) return "";
  const lines: string[] = [];
  pushField(lines, "Type", textValue(event.event_type) || textValue(event.type));
  pushField(lines, "Title", textValue(event.title));
  pushField(lines, "Status", textValue(event.status));
  pushField(lines, "Summary", textValue(event.summary) || textValue(event.message));
  pushJsonBlock(lines, "Payload", objectValue(event.payload));
  return lines.join("\n");
}

function formatSetupRequiredCard(item: ChatTranscriptMessageLike): string {
  const lines: string[] = [];
  pushField(lines, "Agent", textValue(item.agent));
  pushField(lines, "Tool", textValue(item.skill));
  pushField(lines, "Missing required fields", listLabel(stringArray(item.missing_required), "(none)"));
  pushJsonBlock(lines, "Setup", objectValue(item.setup));
  return lines.join("\n");
}

function formatReviewLoopCard(item: ChatTranscriptMessageLike): string {
  const lines: string[] = [];
  pushField(lines, "Job", textValue(item.job_id));
  pushField(lines, "Agent", textValue(item.agent));
  pushField(lines, "Status", textValue(item.status));
  pushField(lines, "Summary", textValue(item.summary));
  pushField(lines, "Critical findings", textValue(item.critical_findings));
  pushField(lines, "Proposed fixes", textValue(item.proposed_fixes));
  pushField(lines, "Promotion frozen", textValue(item.promotion_frozen));

  const events = arrayValue(item.events).map(objectValue).filter((event): event is Record<string, unknown> => Boolean(event));
  if (events.length > 0) {
    lines.push("", "### Events");
    for (const event of events) {
      const eventType = textValue(event.event_type) || "event";
      const status = textValue(event.status);
      const severity = textValue(event.severity);
      const message = textValue(event.message);
      lines.push(`- **${eventType}**${status ? ` (${status})` : ""}${severity ? ` [${severity}]` : ""}${message ? ` - ${message}` : ""}`);
    }
  }

  return lines.join("\n");
}

function formatTranscriptMarkdown(messages: readonly TranscriptMessage[], title: string) {
  const lines = [`# ${oneLine(title) || "Chat transcript"}`, ""];

  for (const message of messages) {
    const timestamp = message.timestamp ? ` - ${message.timestamp}` : "";
    lines.push(`## ${roleLabel(message.role)}${timestamp}`, "", message.content, "");
  }

  return `${lines.join("\n").trimEnd()}\n`;
}

function latestAssistantAnswer(messages: readonly TranscriptMessage[]) {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    if (messages[index].role.trim().toLowerCase() === "assistant") {
      return messages[index].content;
    }
  }
  return "";
}

function downloadMarkdown(markdown: string, name: string) {
  const blob = new Blob([markdown], { type: "text/markdown;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = name;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 0);
}

function markdownFilename(rawName: string) {
  const withoutExtension = rawName.trim().replace(/\.md$/i, "");
  const safeBase = withoutExtension
    .replace(/[^A-Za-z0-9._-]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 80);

  return `${safeBase || "chat-transcript"}.md`;
}

function buttonLabel(status: ToolStatus | null, action: ToolAction, fallback: string) {
  return status?.action === action ? status.label : fallback;
}

function firstText(...values: Array<string | null | undefined>) {
  return values.find((value): value is string => {
    return typeof value === "string" && value.trim().length > 0;
  }) ?? null;
}

function roleLabel(role: string) {
  const normalized = role.trim().toLowerCase();
  if (normalized === "user") return "User";
  if (normalized === "assistant") return "Assistant";
  if (normalized === "system") return "System";
  if (!normalized) return "Message";

  return normalized
    .replace(/[_-]+/g, " ")
    .replace(/\s+/g, " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function cardRoleLabel(kind: string) {
  if (kind === "handoff") return "Handoff grant";
  if (kind === "scope") return "Scope grant";
  if (kind === "dag") return "Agent DAG";
  if (kind === "review_loop") return "Review loop";
  if (kind === "setup_required") return "Setup required";
  return kind;
}

function oneLine(value: string) {
  return value.replace(/\s+/g, " ").trim();
}

function pushField(lines: string[], label: string, value: string | null) {
  if (!value) return;
  lines.push(`**${label}:** ${value}`);
}

function pushJsonBlock(lines: string[], label: string, value: Record<string, unknown> | null) {
  if (!value || Object.keys(value).length === 0) return;
  lines.push("", `### ${label}`, "", "```json", JSON.stringify(value, null, 2), "```");
}

function pushProgress(lines: string[], entries: unknown[]) {
  const progress = entries.map(objectValue).filter((entry): entry is Record<string, unknown> => Boolean(entry));
  if (progress.length === 0) return;
  lines.push("", "### Progress");
  for (const entry of progress) {
    const kind = textValue(entry.kind) || "progress";
    const message = textValue(entry.message) || "";
    lines.push(`- ${kind}${message ? `: ${message}` : ""}`);
  }
}

function pushFileOps(lines: string[], entries: unknown[]) {
  const ops = entries.map(objectValue).filter((entry): entry is Record<string, unknown> => Boolean(entry));
  if (ops.length === 0) return;
  lines.push("", "### File operations");
  for (const op of ops) {
    lines.push(formatFileOp(op));
  }
}

function pushNestedFileOps(lines: string[], entries: unknown[]) {
  const ops = entries.map(objectValue).filter((entry): entry is Record<string, unknown> => Boolean(entry));
  for (const op of ops) {
    lines.push(`  - ${formatFileOp(op).replace(/^- /, "")}`);
  }
}

function formatFileOp(op: Record<string, unknown>) {
  const action = textValue(op.op) || "file";
  const path = textValue(op.path) || "(unknown path)";
  const size = textValue(op.size);
  const contentType = textValue(op.content_type);
  return `- ${action} ${path}${size ? ` (${size} bytes)` : ""}${contentType ? ` - ${contentType}` : ""}`;
}

function writeScopeLabels(scope: Record<string, unknown>): string[] {
  const prefixes = stringArray(scope.write_prefixes);
  if (prefixes.length > 0) return prefixes;
  const writePrefix = textValue(scope.write_prefix);
  if (writePrefix) return [writePrefix];
  const outputsPrefix = textValue(scope.outputs_prefix);
  return outputsPrefix ? [outputsPrefix] : [];
}

function routeLabel(from: string | null, to: string | null) {
  if (from && to) return `${from} -> ${to}`;
  return from || to;
}

function suffix(value: string | null, unit: string) {
  return value ? `${value}${unit}` : null;
}

function listLabel(values: string[], fallback: string) {
  return values.length > 0 ? values.join(", ") : fallback;
}

function textValue(value: unknown): string | null {
  if (typeof value === "string") return value.trim() || null;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return null;
}

function stringArray(value: unknown): string[] {
  return Array.isArray(value)
    ? value.map(textValue).filter((entry): entry is string => Boolean(entry))
    : [];
}

function arrayValue(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function objectValue(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function truthy(value: unknown) {
  return value === true;
}

function cx(...classes: Array<string | false | null | undefined>) {
  return classes.filter(Boolean).join(" ");
}


