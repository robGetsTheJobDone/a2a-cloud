import type { ReactNode } from "react";
import { Virtuoso } from "react-virtuoso";
import type { ChatConnectionState } from "./ChatConnectionStatus";
import { ChatConnectionStatus } from "./ChatConnectionStatus";
import { InlineAlert } from "./DashboardChrome";
import { ChatEmptyState } from "./ChatEmptyState";
import { ChatMessageContent } from "./ChatMessageContent";
import { Icon } from "./Icon";

export type ChatTranscriptItem = {
  id: string;
};

export type ChatTranscriptProps<TItem extends ChatTranscriptItem> = {
  items: TItem[];
  followOutput: "auto" | "smooth";
  streaming: boolean;
  connectionState: ChatConnectionState;
  streamStatus: string | null;
  error: string | null;
  loading?: boolean;
  assistantDraft: string;
  mainModelLabel: string;
  workspaceHasFiles?: boolean;
  workspaceFileCount?: number | null;
  workspaceFileCountHasMore?: boolean;
  canUseMainModel: boolean;
  onCancel: () => void;
  onPickPrompt: (prompt: string) => Promise<void> | void;
  renderItem: (item: TItem, streamingTail: boolean) => ReactNode;
};

export function ChatTranscript<TItem extends ChatTranscriptItem>({
  items,
  followOutput,
  streaming,
  connectionState,
  streamStatus,
  error,
  loading = false,
  assistantDraft,
  mainModelLabel,
  workspaceHasFiles,
  workspaceFileCount,
  workspaceFileCountHasMore = false,
  canUseMainModel,
  onCancel,
  onPickPrompt,
  renderItem,
}: ChatTranscriptProps<TItem>) {
  return (
    <div className="min-h-0 flex-1">
      <Virtuoso
        className="h-full"
        data={items}
        computeItemKey={(_, item) => item.id}
        followOutput={followOutput}
        increaseViewportBy={{ top: 600, bottom: 900 }}
        components={{
          Header: () => (
            <div className="mx-auto max-w-3xl space-y-5 px-4 pt-6 md:px-6">
              {connectionState !== "idle" &&
                connectionState !== "streaming" &&
                connectionState !== "waiting" && (
                  <ChatConnectionStatus
                    state={connectionState}
                    detail={streamStatus || error}
                    onCancel={streaming ? onCancel : undefined}
                  />
                )}
              {loading && items.length === 0 ? (
                <ChatThreadLoadingState />
              ) : items.length === 0 && (
                <ChatEmptyState
                  modelLabel={mainModelLabel}
                  workspaceHasFiles={workspaceHasFiles}
                  workspaceFileCount={workspaceFileCount}
                  workspaceFileCountHasMore={workspaceFileCountHasMore}
                  disabled={streaming || !canUseMainModel}
                  onPickPrompt={(prompt) => void onPickPrompt(prompt)}
                />
              )}
            </div>
          ),
          Footer: () => (
            <div className="mx-auto max-w-3xl space-y-5 px-4 pb-6 pt-5 md:px-6">
              {streaming && (
                <ChatStreamingStatusRow
                  status={streamStatus}
                  draft={assistantDraft}
                />
              )}
              {error && (
                <InlineAlert tone="red" role="alert">
                  {error}
                </InlineAlert>
              )}
            </div>
          ),
        }}
        itemContent={(index, item) => (
          <div className="mx-auto max-w-3xl px-4 pb-5 md:px-6">
            {renderItem(item, streaming && index === items.length - 1)}
          </div>
        )}
      />
    </div>
  );
}

export function ChatThreadLoadingState() {
  return (
    <section
      role="status"
      aria-live="polite"
      aria-label="Loading chat thread"
      className="mx-auto flex w-full max-w-2xl items-start gap-3 rounded-lg border border-runtime-line-soft/60 bg-runtime-bg/70 px-4 py-3 text-left"
    >
      <span className="mt-0.5 h-2 w-2 shrink-0 animate-pulse rounded-full bg-signal-authority shadow-[0_0_8px_rgba(251,191,36,0.45)]" />
      <div className="min-w-0">
        <div className="font-mono text-[11px] uppercase text-ink-muted">
          loading thread
        </div>
        <div className="mt-1 text-sm text-ink-soft">
          Restoring transcript and agent activity.
        </div>
      </div>
    </section>
  );
}

export type ChatMessageRowMessage = {
  role: string;
  content: string;
};

export function ChatMessageRow({
  msg,
  streaming,
}: {
  msg: ChatMessageRowMessage;
  streaming: boolean;
}) {
  const isUser = msg.role === "user";
  if (isUser) {
    return (
      <div className="flex justify-end">
        <div className="max-w-[85%] whitespace-pre-wrap rounded-2xl rounded-tr-md bg-ink px-3.5 py-2 text-[14px] leading-relaxed text-runtime-bg">
          {msg.content ? (
            <ChatMessageContent content={msg.content} markdown={false} />
          ) : null}
        </div>
      </div>
    );
  }
  return (
    <div className="group flex items-start gap-3">
      <span className="mt-0.5 inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-md border border-runtime-line-soft/70 bg-runtime-panel/70 text-ink-soft">
        <Icon name="sparkle" size={13} />
      </span>
      <div className="min-w-0 flex-1 text-[14px] leading-relaxed text-ink">
        {msg.content ? (
          <ChatMessageContent content={msg.content} />
        ) : streaming ? (
          <span className="inline-flex items-center gap-1 text-ink-muted">
            <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-signal-authority" />
            thinking{"\u2026"}
          </span>
        ) : null}
      </div>
    </div>
  );
}

export function ChatStreamingStatusRow({
  status,
  draft,
}: {
  status: string | null;
  draft: string;
}) {
  const label = status || "Working";
  return (
    <div className="flex items-start gap-3">
      <span className="mt-0.5 inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-md border border-runtime-line-soft/70 bg-runtime-panel/70 text-ink-soft">
        <Icon name="sparkle" size={13} />
      </span>
      <div className="min-w-0 flex-1">
        {draft ? (
          <div className="text-[14px] leading-relaxed text-ink">
            <ChatMessageContent content={draft} />
          </div>
        ) : null}
        <div
          className={`flex items-center gap-2 font-mono text-[11px] text-ink-muted ${
            draft ? "mt-2" : ""
          }`}
        >
          <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-signal-authority shadow-[0_0_6px_rgba(251,191,36,0.45)]" />
          <span>{label}</span>
        </div>
      </div>
    </div>
  );
}

export type ChatToolChipEvent = {
  tool: string;
  status: "running" | "ok" | "error";
  summary?: string;
};

export function ChatToolChip({ ev }: { ev: ChatToolChipEvent }) {
  const isRunning = ev.status === "running";
  const isOk = ev.status === "ok";
  const dot = isRunning
    ? "bg-signal-authority animate-pulse"
    : isOk
      ? "bg-signal-live"
      : "bg-signal-danger";
  const tone = isRunning
    ? "border-signal-authority/45 bg-signal-authority/[0.04]"
    : isOk
      ? "border-runtime-line-soft/60 bg-runtime-bg"
      : "border-signal-danger/50 bg-signal-danger/[0.04]";
  return (
    <div className="ml-10 flex items-center gap-2">
      <div
        className={`inline-flex items-center gap-2 rounded-full border px-3 py-1 text-xs ${tone}`}
      >
        <span className={`h-1.5 w-1.5 rounded-full ${dot}`} />
        <span className="font-mono text-ink-soft">{ev.tool}</span>
        {ev.summary && (
          <>
            <Icon name="arrow-right" size={10} className="text-ink-faint" />
            <span className="text-ink-muted">{ev.summary}</span>
          </>
        )}
      </div>
    </div>
  );
}
