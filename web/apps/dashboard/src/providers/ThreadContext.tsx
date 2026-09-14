import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { useSearchParams } from "react-router-dom";
import { deleteThread, listThreads, type ChatThread } from "../api";

const THREAD_PAGE_LIMIT = 30;

/**
 * ThreadContext — single owner of the workspace thread list and the active
 * thread selection. Hoisting selection here means the sidebar, header, and chat
 * body all read/write one source of truth.
 *
 * Mandate B (no state reload on nav): `setActiveThread` switches selection in
 * place and only mirrors the id into the `thread` search param via a `replace`
 * navigation on the SAME route — the workspace route never unmounts, so picking
 * a thread does not reload the page or refetch the thread list. The chat body
 * still loads the newly-selected thread's messages (distinct content), but the
 * shell, list, and providers stay mounted (mandate E).
 *
 * Deep links still hydrate: the initial `activeThreadId` is seeded from the
 * `thread` search param, and external param changes (back/forward, direct URL)
 * flow back into the active selection.
 */
export type ThreadContextValue = {
  /** Currently selected thread id, or null for a fresh/new chat. */
  activeThreadId: string | null;
  /** Loaded thread list (null while the first page is loading). */
  threads: ChatThread[] | null;
  /** Select a thread (or null for a new chat) without leaving the route. */
  setActiveThread: (id: string | null) => void;
  /** Reload the first page of threads. */
  refreshThreads: () => Promise<void>;
  /** Append the next page of threads if more are available. */
  loadMoreThreads: () => void;
  /** Whether more thread pages can be loaded. */
  hasMore: boolean;
  /** Whether a thread-list request is in flight. */
  loading: boolean;
  /** Whether a subsequent page is currently loading. */
  loadingMore: boolean;
  /** Delete a thread; clears the active selection if it was active. */
  removeThread: (id: string) => Promise<void>;
};

const ThreadContext = createContext<ThreadContextValue | null>(null);

export function ThreadProvider({ children }: { children: ReactNode }) {
  const [searchParams, setSearchParams] = useSearchParams();
  const routeThreadId = searchParams.get("thread") || null;

  const [activeThreadId, setActiveThreadId] = useState<string | null>(
    () => routeThreadId,
  );
  const [threads, setThreads] = useState<ChatThread[] | null>(null);
  const [cursor, setCursor] = useState<string | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(false);
  const loadingRef = useRef(false);
  const lastRouteThreadIdRef = useRef<string | null>(routeThreadId);

  // Deep-link / browser-navigation hydration: an external `thread` param change
  // flows into the active selection without remounting the route.
  useEffect(() => {
    if (lastRouteThreadIdRef.current === routeThreadId) return;
    lastRouteThreadIdRef.current = routeThreadId;
    setActiveThreadId(routeThreadId);
  }, [routeThreadId]);

  const setActiveThread = useCallback(
    (id: string | null) => {
      lastRouteThreadIdRef.current = id;
      setActiveThreadId(id);
      setSearchParams(
        (current) => {
          const next = new URLSearchParams(current);
          if (id) next.set("thread", id);
          else next.delete("thread");
          return next;
        },
        { replace: true },
      );
    },
    [setSearchParams],
  );

  const loadThreads = useCallback(
    async (nextCursor: string | null, append: boolean) => {
      if (loadingRef.current) return;
      loadingRef.current = true;
      setLoading(true);
      try {
        const page = await listThreads({
          limit: THREAD_PAGE_LIMIT,
          cursor: nextCursor,
        });
        setThreads((current) => {
          if (!append) return page.items;
          const seen = new Set((current ?? []).map((thread) => thread.id));
          return [
            ...(current ?? []),
            ...page.items.filter((thread) => !seen.has(thread.id)),
          ];
        });
        setCursor(page.next_cursor);
        setHasMore(Boolean(page.has_more ?? page.next_cursor));
      } catch {
        if (!append) setThreads((current) => current ?? []);
      } finally {
        loadingRef.current = false;
        setLoading(false);
      }
    },
    [],
  );

  const refreshThreads = useCallback(() => loadThreads(null, false), [loadThreads]);

  const loadMoreThreads = useCallback(() => {
    if (!cursor || !hasMore || loadingRef.current) return;
    void loadThreads(cursor, true);
  }, [cursor, hasMore, loadThreads]);

  const removeThread = useCallback(
    async (id: string) => {
      try {
        await deleteThread(id);
      } catch {
        /* ignore — the list refresh reconciles real state */
      }
      if (activeThreadId === id) setActiveThread(null);
      void refreshThreads();
    },
    [activeThreadId, refreshThreads, setActiveThread],
  );

  useEffect(() => {
    void refreshThreads();
  }, [refreshThreads]);

  const value: ThreadContextValue = {
    activeThreadId,
    threads,
    setActiveThread,
    refreshThreads,
    loadMoreThreads,
    hasMore,
    loading,
    loadingMore: loading && Boolean(cursor),
    removeThread,
  };

  return <ThreadContext.Provider value={value}>{children}</ThreadContext.Provider>;
}

/**
 * useThreadContext — read the workspace thread list + active selection. Must be
 * called inside a <ThreadProvider>.
 */
export function useThreadContext(): ThreadContextValue {
  const ctx = useContext(ThreadContext);
  if (!ctx) {
    throw new Error("useThreadContext must be used within a ThreadProvider");
  }
  return ctx;
}
