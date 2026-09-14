import { useCallback, useEffect, useRef, useState } from "react";
import {
  cancelJob,
  chatStream,
  getThreadActivity,
  getThreadMessages,
  type ChatMessage,
  type ChatThreadSettings,
  type MainLlmSource,
} from "../api";
import type { ChatAttachment } from "./ChatComposer";
import type { ChatConnectionState } from "./ChatConnectionStatus";
import { shouldDeferThreadContentLoad } from "./chatThreadLoad";
import type { ChatMainModelStatus } from "./chatMainModelStatus";
import {
  useChatEventDispatcher,
  type ChatEventDispatcher,
  type Item,
} from "./ChatEventDispatcher";

const THREAD_ACTIVITY_INITIAL_LIMIT = 120;
const STREAM_STATUS_FLUSH_MS = 120;

function formatBytes(bytes: number) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function buildUserMessageContent(text: string, attachments: ChatAttachment[]) {
  if (attachments.length === 0) return text;
  const fileLines = attachments.map(
    (file) =>
      `- ${file.uri} (${file.name}, ${formatBytes(file.size_bytes)}, ${file.mime_type})`,
  );
  const header = "Attached workspace files:";
  return [text, [header, ...fileLines].join("\n")].filter(Boolean).join("\n\n");
}

export type ChatStreamControllerOptions = {
  /** Active thread id from ThreadContext (null = new chat). */
  threadId: string | null;
  /** Promote a freshly-created thread id into the active selection. */
  onThreadCreated: (id: string) => void;
  /** Refresh the thread list (after new threads / completed runs). */
  refreshThreads: () => void;
  /** Main-model gating + request credential resolution. */
  mainModelStatus: ChatMainModelStatus;
  approvalMode: boolean;
  mainLlmSource: MainLlmSource;
  /** Slug for the active chat organization, if any. */
  organizationSlug: () => string | undefined;
  /** Thread-scoped policy overrides to send with each run. */
  threadPolicyOverrides: () => ChatThreadSettings | undefined;
  /** Apply settings returned by the backend to local override state. */
  applyThreadSettings: (settings: ChatThreadSettings | undefined) => void;
};

export type ChatStreamController = {
  items: Item[];
  streaming: boolean;
  streamStatus: string | null;
  assistantDraft: string;
  err: string | null;
  connectionState: ChatConnectionState;
  threadContentLoading: boolean;
  loadedThreadTitle: string | null;
  send: (textInput: string, attachments?: ChatAttachment[]) => Promise<void>;
  cancel: () => Promise<void>;
};

/**
 * useChatStreamController — owns the live chat stream lifecycle for the active
 * thread: connecting to {@link chatStream}, buffering token deltas, flushing
 * stream-status, committing the assistant reply, cancellation, and hydrating a
 * thread's persisted messages + activity on selection.
 *
 * It composes {@link useChatEventDispatcher} (the transcript reducer) and feeds
 * it the connection-state / status / draft side-effects. Business logic and
 * network calls are unchanged from the original monolithic Chat component.
 */
export function useChatStreamController(
  options: ChatStreamControllerOptions,
): ChatStreamController {
  const {
    threadId,
    onThreadCreated,
    refreshThreads,
    mainModelStatus,
    approvalMode,
    mainLlmSource,
    organizationSlug,
    threadPolicyOverrides,
    applyThreadSettings,
  } = options;

  const [streaming, setStreaming] = useState(false);
  const [streamStatus, setStreamStatusState] = useState<string | null>(null);
  const [assistantDraft, setAssistantDraft] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [connectionState, setConnectionState] = useState<ChatConnectionState>("idle");
  const [threadContentLoading, setThreadContentLoading] = useState(false);
  const [loadedThreadTitle, setLoadedThreadTitle] = useState<string | null>(null);
  const [reloadToken, setReloadToken] = useState(0);

  const abortRef = useRef<AbortController | null>(null);
  const activeJobIdRef = useRef<string | null>(null);
  const localStreamThreadIdRef = useRef<string | null>(null);
  const connectionStateRef = useRef<ChatConnectionState>("idle");
  connectionStateRef.current = connectionState;

  // Streaming deltas arrive per-token; buffer them in a ref and flush to state
  // at most every ~80ms so each token doesn't trigger a full re-render.
  const draftBufRef = useRef("");
  const draftFlushTimerRef = useRef<number | null>(null);
  const pendingStreamStatusRef = useRef<string | null>(null);
  const streamStatusFlushTimerRef = useRef<number | null>(null);

  useEffect(() => {
    return () => {
      if (draftFlushTimerRef.current !== null) {
        window.clearTimeout(draftFlushTimerRef.current);
        draftFlushTimerRef.current = null;
      }
      if (streamStatusFlushTimerRef.current !== null) {
        window.clearTimeout(streamStatusFlushTimerRef.current);
        streamStatusFlushTimerRef.current = null;
      }
    };
  }, []);

  const clearQueuedStreamStatus = useCallback(() => {
    pendingStreamStatusRef.current = null;
    if (streamStatusFlushTimerRef.current !== null) {
      window.clearTimeout(streamStatusFlushTimerRef.current);
      streamStatusFlushTimerRef.current = null;
    }
  }, []);

  const setStreamStatusNow = useCallback(
    (status: string | null | ((current: string | null) => string | null)) => {
      clearQueuedStreamStatus();
      setStreamStatusState(status);
    },
    [clearQueuedStreamStatus],
  );

  const queueStreamStatus = useCallback((status: string) => {
    pendingStreamStatusRef.current = status;
    if (streamStatusFlushTimerRef.current !== null) return;
    streamStatusFlushTimerRef.current = window.setTimeout(() => {
      streamStatusFlushTimerRef.current = null;
      const next = pendingStreamStatusRef.current;
      pendingStreamStatusRef.current = null;
      if (next === null) return;
      setStreamStatusState((current) => (current === next ? current : next));
    }, STREAM_STATUS_FLUSH_MS);
  }, []);

  const queueAssistantDelta = useCallback((content: string) => {
    draftBufRef.current += content;
    if (draftFlushTimerRef.current !== null) return;
    draftFlushTimerRef.current = window.setTimeout(() => {
      draftFlushTimerRef.current = null;
      const buf = draftBufRef.current;
      if (!buf) return;
      draftBufRef.current = "";
      setAssistantDraft((cur) => cur + buf);
    }, 80);
  }, []);

  const resetAssistantDraft = useCallback((content = "") => {
    if (draftFlushTimerRef.current !== null) {
      window.clearTimeout(draftFlushTimerRef.current);
      draftFlushTimerRef.current = null;
    }
    draftBufRef.current = "";
    setAssistantDraft(content);
  }, []);

  // Stable dispatcher effects: the transcript reducer reports connection-state,
  // status, deltas, and errors back through these callbacks.
  const dispatcherEffectsRef = useRef({
    onConnectionState: (state: ChatConnectionState) => setConnectionState(state),
    onStreamStatus: (status: string, replay: boolean) => {
      if (!replay) queueStreamStatus(status);
    },
    onAssistantDelta: (content: string) => queueAssistantDelta(content),
    onAssistantFinal: (content: string) => resetAssistantDraft(content),
    onError: (message: string) => {
      setErr(message);
      setStreamStatusNow("Run failed");
    },
  });
  dispatcherEffectsRef.current.onStreamStatus = (status: string, replay: boolean) => {
    if (!replay) queueStreamStatus(status);
  };
  dispatcherEffectsRef.current.onAssistantDelta = (content: string) =>
    queueAssistantDelta(content);
  dispatcherEffectsRef.current.onAssistantFinal = (content: string) =>
    resetAssistantDraft(content);
  dispatcherEffectsRef.current.onError = (message: string) => {
    setErr(message);
    setStreamStatusNow("Run failed");
  };

  const dispatcher: ChatEventDispatcher = useChatEventDispatcher(
    dispatcherEffectsRef.current,
  );
  const { items, loadHistory, pushItem, resetItems, dispatchEvent } = dispatcher;

  // Keep a stable view of message history for building outgoing requests.
  const messagesRef = useRef(dispatcher.messages);
  messagesRef.current = dispatcher.messages;

  // Load thread on mount, when the active thread changes, or when an explicit
  // reload is requested (e.g. re-picking the active thread).
  useEffect(() => {
    if (!threadId) {
      resetItems();
      setLoadedThreadTitle(null);
      setThreadContentLoading(false);
      return;
    }
    if (
      shouldDeferThreadContentLoad({
        threadId,
        streaming,
        localStreamThreadId: localStreamThreadIdRef.current,
      })
    ) {
      setThreadContentLoading(false);
      return;
    }
    let cancelled = false;
    if (!streaming) {
      resetItems();
      setLoadedThreadTitle(null);
      setThreadContentLoading(true);
    }
    Promise.all([
      getThreadMessages(threadId),
      getThreadActivity(threadId, { limit: THREAD_ACTIVITY_INITIAL_LIMIT }).catch(
        () => null,
      ),
    ])
      .then(([d, activity]) => {
        if (cancelled) return;
        applyThreadSettings(d.settings);
        setLoadedThreadTitle(d.title?.trim() || activity?.title?.trim() || null);
        loadHistory(d.messages.map((m) => ({ role: m.role, content: m.content })));
        for (const event of activity?.events || []) {
          dispatchEvent(event, { replay: true });
        }
      })
      .catch((ex) => {
        if (!cancelled) setErr(`load thread: ${ex.message ?? ex}`);
      })
      .finally(() => {
        if (!cancelled) setThreadContentLoading(false);
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [threadId, reloadToken]);

  const send = useCallback(
    async (textInput: string, attachments: ChatAttachment[] = []) => {
      const text = textInput.trim();
      if ((!text && attachments.length === 0) || streaming || threadContentLoading) return;
      if (!mainModelStatus.canChat) {
        setErr(mainModelStatus.blocker || "Add an LLM key before chatting.");
        setConnectionState("error");
        setStreamStatusNow("LLM key required");
        return;
      }
      const userContent = buildUserMessageContent(text, attachments);
      setErr(null);
      pushItem({ kind: "msg", role: "user", content: userContent });
      setStreaming(true);
      setConnectionState("connecting");
      setStreamStatusNow("Connecting to main agent");
      resetAssistantDraft();

      const history: ChatMessage[] = messagesRef.current.map((it) => ({
        role: it.role,
        content: it.content,
      }));
      history.push({ role: "user", content: userContent });

      abortRef.current = new AbortController();
      activeJobIdRef.current = null;
      let assistantBuf = "";
      let committed = false;
      let aborted = false;
      let resolvedThreadId = threadId;
      const effectiveMainLlmSource: MainLlmSource = mainLlmSource;
      try {
        for await (const ev of chatStream(
          history,
          abortRef.current.signal,
          approvalMode,
          effectiveMainLlmSource,
          mainModelStatus.requestCredsName,
          threadId || undefined,
          threadPolicyOverrides(),
          organizationSlug() || undefined,
        )) {
          if (ev.type === "thread") {
            activeJobIdRef.current = ev.job_id || null;
            resolvedThreadId = ev.id;
            if (!threadId) {
              localStreamThreadIdRef.current = ev.id;
              onThreadCreated(ev.id);
            }
            if (ev.settings) applyThreadSettings(ev.settings);
            setStreamStatusNow(ev.is_new ? "Started new thread" : "Preparing response");
            if (ev.is_new) refreshThreads();
            continue;
          }
          dispatchEvent(ev);
          if (ev.type === "final" && ev.content) {
            assistantBuf = ev.content;
            if (!committed) {
              pushItem({ kind: "msg", role: "assistant", content: assistantBuf });
              resetAssistantDraft();
              committed = true;
            }
          }
        }
        if (assistantBuf && !committed) {
          pushItem({ kind: "msg", role: "assistant", content: assistantBuf });
          committed = true;
        }
        refreshThreads();
        setConnectionState("idle");
        activeJobIdRef.current = null;
      } catch (ex: unknown) {
        if ((ex as { name?: string }).name === "AbortError") {
          aborted = true;
          setStreamStatusNow("Stopped");
          setConnectionState("stopped");
        } else {
          setErr(ex instanceof Error ? ex.message : String(ex));
          setStreamStatusNow("Stream failed");
          setConnectionState("error");
        }
      } finally {
        setStreaming(false);
        resetAssistantDraft();
        if (!committed && !aborted) {
          if (assistantBuf) {
            pushItem({ kind: "msg", role: "assistant", content: assistantBuf });
            committed = true;
          } else if (resolvedThreadId) {
            setReloadToken((token) => token + 1);
          }
        }
        setStreamStatusNow((current) =>
          current === "Stopped" || current === "Stream failed" ? current : null,
        );
        abortRef.current = null;
        localStreamThreadIdRef.current = null;
        if (connectionStateRef.current !== "stopped") {
          activeJobIdRef.current = null;
        }
      }
    },
    [
      applyThreadSettings,
      approvalMode,
      dispatchEvent,
      mainLlmSource,
      mainModelStatus.blocker,
      mainModelStatus.canChat,
      mainModelStatus.requestCredsName,
      onThreadCreated,
      organizationSlug,
      pushItem,
      refreshThreads,
      resetAssistantDraft,
      setStreamStatusNow,
      streaming,
      threadContentLoading,
      threadId,
      threadPolicyOverrides,
    ],
  );

  const cancel = useCallback(async () => {
    const jobId = activeJobIdRef.current;
    abortRef.current?.abort();
    if (jobId) {
      try {
        await cancelJob(jobId, "canceled from dashboard");
      } catch {
        // Local abort still stops the stream; the job may already be terminal.
      }
    }
  }, []);

  return {
    items,
    streaming,
    streamStatus,
    assistantDraft,
    err,
    connectionState,
    threadContentLoading,
    loadedThreadTitle,
    send,
    cancel,
  };
}
