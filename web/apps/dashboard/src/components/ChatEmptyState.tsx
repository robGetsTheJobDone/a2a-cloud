import { Icon } from "./Icon";
import { ChatPromptHints } from "./ChatPromptHints";

export type ChatEmptyStateProps = {
  modelLabel: string;
  workspaceHasFiles?: boolean;
  workspaceFileCount: number | null | undefined;
  workspaceFileCountHasMore?: boolean;
  disabled?: boolean;
  onPickPrompt: (prompt: string) => void;
};

export function ChatEmptyState({
  modelLabel,
  workspaceHasFiles,
  workspaceFileCount,
  workspaceFileCountHasMore = false,
  disabled = false,
  onPickPrompt,
}: ChatEmptyStateProps) {
  const hasWorkspaceFiles =
    workspaceHasFiles ??
    (typeof workspaceFileCount === "number" ? workspaceFileCount > 0 : undefined);

  return (
    <section
      aria-label="New chat"
      className="mx-auto flex w-full max-w-xl flex-col items-center px-4 pt-10 sm:pt-16"
    >
      <div className="flex flex-col items-center text-center">
        <span className="relative inline-flex h-12 w-12 items-center justify-center rounded-2xl border border-signal-protocol/35 bg-signal-protocol/10 text-signal-protocol">
          <Icon name="sparkle" size={20} />
          <span className="absolute -bottom-1 left-1/2 h-px w-8 -translate-x-1/2 bg-signal-protocol/40" />
        </span>
        <h1 className="mt-5 text-2xl font-semibold tracking-tight text-ink sm:text-[28px]">
          What do you want to get done?
        </h1>
        <p className="mt-2 max-w-sm text-sm leading-6 text-ink-muted">
          Describe the outcome. Your conversation, files, and live work stay together here.
        </p>
        <span className="mt-3 inline-flex items-center gap-1.5 rounded-full border border-runtime-line-soft/70 bg-runtime-panel/55 px-2.5 py-1 font-mono text-[11px] text-ink-faint">
          <span className="h-1.5 w-1.5 rounded-full bg-signal-live" aria-hidden />
          {modelLabel}
        </span>
      </div>

      <ChatPromptHints
        disabled={disabled}
        hasAvailableFiles={hasWorkspaceFiles}
        availableFileCount={workspaceFileCount}
        availableFileCountHasMore={workspaceFileCountHasMore}
        onPickPrompt={onPickPrompt}
        maxPrompts={4}
        className="mt-8"
      />
    </section>
  );
}
