import { useEffect, useId, useMemo, useRef, useState } from "react";
import { Virtuoso } from "react-virtuoso";
import type { ChatThread } from "../api";
import { EmptyState, InlineAlert, TextInput, ToolbarButton } from "./DashboardChrome";
import { Icon } from "./Icon";

export type ChatThreadSidebarProps = {
  threads: ChatThread[] | null | undefined;
  activeId: string | null;
  /**
   * Select a thread. Wired to ThreadContext.setActiveThread — switches the
   * active thread in place without a route change or thread-list refetch
   * (mandate B). The workspace route never unmounts.
   */
  onPick: (id: string) => void;
  onNew: () => void;
  onDelete: (id: string) => void | Promise<void>;
  /** Open the thread's settings as an in-place DetailSheet (mandate C). */
  onOpenSettings?: (id: string) => void;
  onLoadMore?: () => void;
  hasMore?: boolean;
  loadingMore?: boolean;
  desktopOpen?: boolean;
  onDesktopOpenChange?: (open: boolean) => void;
  mobileOpen?: boolean;
  onMobileOpenChange?: (open: boolean) => void;
};

type DeleteTarget = {
  id: string;
  title: string;
};

type ThreadGroup = {
  label: string;
  threads: ChatThread[];
};

type ThreadListEntry =
  | { kind: "heading"; id: string; label: string }
  | { kind: "thread"; id: string; thread: ChatThread };

function cx(...classes: Array<string | false | null | undefined>) {
  return classes.filter(Boolean).join(" ");
}

function displayTitle(thread: Pick<ChatThread, "title">) {
  return thread.title?.trim() || "Untitled chat";
}

function relativeUpdated(value: string) {
  const time = Date.parse(value);
  if (!Number.isFinite(time)) return null;
  const diff = Date.now() - time;
  const minute = 60_000;
  const hour = 60 * minute;
  const day = 24 * hour;
  if (diff < minute) return "just now";
  if (diff < hour) return `${Math.round(diff / minute)}m ago`;
  if (diff < day) return `${Math.round(diff / hour)}h ago`;
  if (diff < 7 * day) return `${Math.round(diff / day)}d ago`;
  return new Date(time).toLocaleDateString();
}

function groupByRecency(threads: ChatThread[]): ThreadGroup[] {
  const day = 24 * 60 * 60 * 1000;
  const now = Date.now();
  const buckets: { label: string; max: number; items: ChatThread[] }[] = [
    { label: "Today", max: day, items: [] },
    { label: "Yesterday", max: 2 * day, items: [] },
    { label: "This week", max: 7 * day, items: [] },
    { label: "Earlier", max: Infinity, items: [] },
  ];
  for (const thread of threads) {
    const time = Date.parse(thread.updated_at);
    const delta = Number.isFinite(time) ? now - time : Infinity;
    const bucket = buckets.find((b) => delta < b.max) ?? buckets[buckets.length - 1];
    bucket.items.push(thread);
  }
  return buckets
    .filter((b) => b.items.length > 0)
    .map((b) => ({ label: b.label, threads: b.items }));
}

function flattenThreadGroups(groups: ThreadGroup[]): ThreadListEntry[] {
  return groups.flatMap((group) => [
    { kind: "heading" as const, id: `heading:${group.label}`, label: group.label },
    ...group.threads.map((thread) => ({
      kind: "thread" as const,
      id: thread.id,
      thread,
    })),
  ]);
}

export function ChatThreadSidebar({
  threads,
  activeId,
  onPick,
  onNew,
  onDelete,
  onOpenSettings,
  onLoadMore,
  hasMore = false,
  loadingMore = false,
  desktopOpen = true,
  onDesktopOpenChange,
  mobileOpen,
  onMobileOpenChange,
}: ChatThreadSidebarProps) {
  const [internalMobileOpen, setInternalMobileOpen] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<DeleteTarget | null>(null);
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const dialogTitleId = useId();
  const mobileCloseRef = useRef<HTMLButtonElement>(null);
  const cancelDeleteRef = useRef<HTMLButtonElement>(null);
  const list = threads ?? [];
  const isLoading = threads == null;
  const mobileDrawerOpen = mobileOpen ?? internalMobileOpen;

  function setMobileDrawerOpen(open: boolean) {
    onMobileOpenChange?.(open);
    if (mobileOpen === undefined) setInternalMobileOpen(open);
  }

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return list;
    return list.filter((thread) =>
      displayTitle(thread).toLowerCase().includes(q),
    );
  }, [list, query]);

  const groups = useMemo(() => groupByRecency(filtered), [filtered]);

  useEffect(() => {
    if (!mobileDrawerOpen) return undefined;
    const previousFocus =
      document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const timeoutId = window.setTimeout(() => mobileCloseRef.current?.focus(), 0);
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setMobileDrawerOpen(false);
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      window.clearTimeout(timeoutId);
      document.removeEventListener("keydown", handleKeyDown);
      previousFocus?.focus();
    };
  }, [mobileDrawerOpen]);

  useEffect(() => {
    if (!deleteTarget) return undefined;
    const previousFocus =
      document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const timeoutId = window.setTimeout(() => cancelDeleteRef.current?.focus(), 0);
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !deleteBusy) {
        setDeleteTarget(null);
        setDeleteError(null);
      }
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      window.clearTimeout(timeoutId);
      document.removeEventListener("keydown", handleKeyDown);
      previousFocus?.focus();
    };
  }, [deleteBusy, deleteTarget]);

  function pickThread(id: string) {
    onPick(id);
    setMobileDrawerOpen(false);
  }

  function startNewThread() {
    onNew();
    setMobileDrawerOpen(false);
  }

  function requestDelete(thread: ChatThread) {
    setDeleteTarget({ id: thread.id, title: displayTitle(thread) });
    setDeleteError(null);
  }

  async function confirmDelete() {
    if (!deleteTarget || deleteBusy) return;
    setDeleteBusy(true);
    setDeleteError(null);
    try {
      await onDelete(deleteTarget.id);
      setDeleteTarget(null);
      setMobileDrawerOpen(false);
    } catch (error) {
      setDeleteError(error instanceof Error ? error.message : String(error));
    } finally {
      setDeleteBusy(false);
    }
  }

  function renderPanel(onCollapse?: () => void) {
    return (
      <ThreadPanel
        groups={groups}
        total={list.length}
        activeId={activeId}
        isLoading={isLoading}
        query={query}
        onQueryChange={setQuery}
        onPick={pickThread}
        onNew={startNewThread}
        onDelete={requestDelete}
        onOpenSettings={onOpenSettings}
        onLoadMore={onLoadMore}
        hasMore={hasMore}
        loadingMore={loadingMore}
        onCollapse={onCollapse}
      />
    );
  }

  return (
    <>
      {desktopOpen && (
        <aside className="hidden w-64 shrink-0 border-r border-runtime-line-soft/70 bg-runtime-bg/85 md:flex md:flex-col xl:w-72">
          {renderPanel(onDesktopOpenChange ? () => onDesktopOpenChange(false) : undefined)}
        </aside>
      )}

      {mobileDrawerOpen && (
        <div
          className="fixed inset-0 z-40 bg-runtime-bg/70 md:hidden"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) setMobileDrawerOpen(false);
          }}
        >
          <aside
            id="chat-thread-mobile-drawer"
            className="flex h-full w-[min(22rem,calc(100vw-2rem))] flex-col border-r border-runtime-line-soft/60 bg-runtime-bg shadow-2xl shadow-black"
          >
            <div className="flex items-center justify-between border-b border-runtime-line-soft/60 px-3 py-2.5">
              <div className="font-mono text-[11px] uppercase text-ink-muted">
                threads
              </div>
              <ToolbarButton
                ref={mobileCloseRef}
                aria-label="Close threads"
                onClick={() => setMobileDrawerOpen(false)}
                variant="ghost"
                size="xs"
                className="h-7 w-7 px-0"
              >
                <Icon name="close" size={14} />
              </ToolbarButton>
            </div>
            {renderPanel()}
          </aside>
        </div>
      )}

      {deleteTarget && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-runtime-bg/70 p-4">
          <div
            role="dialog"
            aria-modal="true"
            aria-labelledby={dialogTitleId}
            className="w-full max-w-sm overflow-hidden rounded-lg border border-runtime-line-soft/60 bg-runtime-bg shadow-2xl shadow-black"
          >
            <div className="border-b border-runtime-line-soft/60 px-4 py-3">
              <h2 id={dialogTitleId} className="text-sm font-semibold text-ink">
                Delete this chat?
              </h2>
              <p className="mt-1 break-words text-xs leading-relaxed text-ink-muted">
                Removes <span className="font-mono text-ink-soft">"{deleteTarget.title}"</span> from your thread list. This cannot be undone.
              </p>
            </div>
            {deleteError && (
              <InlineAlert tone="red" role="alert" className="mx-4 mt-3 text-xs">
                {deleteError}
              </InlineAlert>
            )}
            <div className="flex justify-end gap-2 px-4 py-3">
              <ToolbarButton
                ref={cancelDeleteRef}
                disabled={deleteBusy}
                onClick={() => {
                  setDeleteTarget(null);
                  setDeleteError(null);
                }}
                size="xs"
              >
                Cancel
              </ToolbarButton>
              <ToolbarButton
                disabled={deleteBusy}
                onClick={confirmDelete}
                variant="danger"
                size="xs"
              >
                <Icon name="trash" size={12} />
                {deleteBusy ? "Deleting..." : "Delete"}
              </ToolbarButton>
            </div>
          </div>
        </div>
      )}
    </>
  );
}

function ThreadPanel({
  groups,
  total,
  activeId,
  isLoading,
  query,
  onQueryChange,
  onPick,
  onNew,
  onDelete,
  onOpenSettings,
  onLoadMore,
  hasMore,
  loadingMore,
  onCollapse,
}: {
  groups: ThreadGroup[];
  total: number;
  activeId: string | null;
  isLoading: boolean;
  query: string;
  onQueryChange: (q: string) => void;
  onPick: (id: string) => void;
  onNew: () => void;
  onDelete: (thread: ChatThread) => void;
  onOpenSettings?: (id: string) => void;
  onLoadMore?: () => void;
  hasMore: boolean;
  loadingMore: boolean;
  onCollapse?: () => void;
}) {
  const entries = useMemo(() => flattenThreadGroups(groups), [groups]);

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="border-b border-runtime-line-soft/60 px-3 py-3">
        <div className="flex items-center justify-between">
          <div>
            <div className="font-mono text-[9px] uppercase tracking-[0.16em] text-ink-faint">
              task history
            </div>
            <div className="mt-0.5 text-sm font-semibold text-ink">
              Your tasks {total > 0 && <span className="font-mono text-[10px] font-normal text-ink-faint">· {total}</span>}
            </div>
          </div>
          <div className="flex items-center gap-1">
            <ToolbarButton
              onClick={onNew}
              aria-label="New chat"
              title="New chat"
              variant="primary"
              size="xs"
            >
              <Icon name="plus" size={12} strokeWidth={2.2} />
              New task
            </ToolbarButton>
            {onCollapse && (
              <ToolbarButton
                onClick={onCollapse}
                aria-label="Collapse threads"
                title="Collapse threads"
                variant="ghost"
                size="xs"
                className="h-7 w-7 px-0"
              >
                <Icon name="panel-left" size={13} />
              </ToolbarButton>
            )}
          </div>
        </div>
        <div className="relative mt-2.5">
          <Icon
            name="search"
            size={13}
            className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-ink-faint"
          />
          <TextInput
            type="search"
            value={query}
            onChange={(e) => onQueryChange(e.target.value)}
            placeholder="Find a task…"
            aria-label="Search tasks"
            compact
            className="pl-8 pr-2"
          />
        </div>
      </div>

      <nav aria-label="Chat threads" className="min-h-0 flex-1 px-2 py-2">
        {isLoading ? (
          <LoadingThreads />
        ) : total === 0 ? (
          <EmptyThreads onNew={onNew} />
        ) : groups.length === 0 ? (
          <div className="px-3 py-6 text-center text-xs text-ink-muted">
            No threads match{" "}
            <span className="font-mono text-ink-soft">"{query}"</span>.
          </div>
        ) : (
          <Virtuoso
            className="h-full"
            data={entries}
            endReached={() => {
              if (hasMore && !loadingMore) onLoadMore?.();
            }}
            increaseViewportBy={240}
            computeItemKey={(_, entry) => entry.id}
            components={{
              Footer: () => (
                loadingMore ? (
                  <div className="px-2 py-2 font-mono text-[10px] uppercase text-ink-faint">
                    loading more…
                  </div>
                ) : null
              ),
            }}
            itemContent={(index, entry) => (
              entry.kind === "heading" ? (
                <div
                  role="presentation"
                  className={cx(
                    "px-2 pb-1.5 font-mono text-[10px] uppercase text-ink-faint",
                    index === 0 ? "pt-0" : "pt-3",
                  )}
                >
                  {entry.label}
                </div>
              ) : (
                <div role="listitem" className="pb-0.5">
                  <ThreadListItem
                    thread={entry.thread}
                    active={activeId === entry.thread.id}
                    onPick={onPick}
                    onDelete={onDelete}
                    onOpenSettings={onOpenSettings}
                  />
                </div>
              )
            )}
          />
        )}
      </nav>
    </div>
  );
}

function ThreadListItem({
  thread,
  active,
  onPick,
  onDelete,
  onOpenSettings,
}: {
  thread: ChatThread;
  active: boolean;
  onPick: (id: string) => void;
  onDelete: (thread: ChatThread) => void;
  onOpenSettings?: (id: string) => void;
}) {
  const title = displayTitle(thread);
  const updated = relativeUpdated(thread.updated_at);
  const emailThread = thread.settings?.source === "email";
  const emailTooltip = thread.settings?.remote_address
    ? `Email thread from ${thread.settings.remote_address}`
    : "Email thread";

  return (
    <div
      className={cx(
        "group relative grid items-center rounded-md transition",
        onOpenSettings
          ? "grid-cols-[minmax(0,1fr)_1.75rem_1.75rem]"
          : "grid-cols-[minmax(0,1fr)_1.75rem]",
        active
          ? "bg-signal-protocol/[0.08] ring-1 ring-signal-protocol/30"
          : "hover:bg-runtime-panel",
      )}
    >
      {active && (
        <span
          aria-hidden
          className="absolute inset-y-1 left-0 w-0.5 rounded-full bg-signal-protocol"
        />
      )}
      <button
        type="button"
        onClick={() => onPick(thread.id)}
        aria-current={active ? "page" : undefined}
        title={title}
        className="min-w-0 px-3 py-2 text-left focus:outline-none"
      >
        <span className="flex min-w-0 items-center gap-1.5">
          {emailThread && (
            <span
              title={emailTooltip}
              aria-label={emailTooltip}
              className="shrink-0 text-ink-faint"
            >
              <Icon name="mail" size={12} />
            </span>
          )}
          <span
            className={cx(
              "block min-w-0 truncate text-[13px] font-medium",
              active ? "text-ink" : "text-ink-soft",
            )}
          >
            {title}
          </span>
        </span>
        {updated && (
          <span className="mt-0.5 block truncate font-mono text-[10px] text-ink-faint">
            {updated}
          </span>
        )}
      </button>
      {onOpenSettings && (
        <ToolbarButton
          aria-label={`Settings for ${title}`}
          title="Thread settings"
          onClick={() => onOpenSettings(thread.id)}
          variant="ghost"
          size="xs"
          className="h-7 w-7 border-transparent bg-transparent px-0 text-ink-faint opacity-0 group-hover:opacity-100 group-focus-within:opacity-100"
        >
          <Icon name="sliders" size={13} />
        </ToolbarButton>
      )}
      <ToolbarButton
        aria-label={`Delete ${title}`}
        title="Delete thread"
        onClick={() => onDelete(thread)}
        variant="danger"
        size="xs"
        className="mr-1 h-7 w-7 border-transparent bg-transparent px-0 text-ink-faint opacity-0 group-hover:opacity-100 group-focus-within:opacity-100"
      >
        <Icon name="trash" size={13} />
      </ToolbarButton>
    </div>
  );
}

function LoadingThreads() {
  return (
    <div role="status" aria-live="polite" className="space-y-2 px-1 py-2">
      <div className="font-mono text-[10px] uppercase text-ink-faint">
        loading…
      </div>
      {[0, 1, 2, 3].map((item) => (
        <div
          key={item}
          className="h-11 animate-pulse rounded-md bg-runtime-panel/60"
        />
      ))}
    </div>
  );
}

function EmptyThreads({ onNew }: { onNew: () => void }) {
  return (
    <EmptyState
      title="No threads yet"
      description="Start a conversation. Threads auto-save and stay here."
      size="compact"
      className="mx-2 mt-3"
      action={
        <ToolbarButton type="button" onClick={onNew} variant="primary">
          <Icon name="plus" size={12} strokeWidth={2.2} />
          Start chat
        </ToolbarButton>
      }
    />
  );
}


