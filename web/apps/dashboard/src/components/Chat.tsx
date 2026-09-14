import { useEffect, useMemo, useRef, useState } from "react";
import {
  hasWorkspaceFiles,
  listLlmCreds,
  listOrganizations,
  updateThreadSettings,
  type ChatThreadSettings,
  type LLMCreds,
  type MainLlmSource,
  type Organization,
} from "../api";
import { ChatComposer } from "./ChatComposer";
import { resolveChatMainModelStatus } from "./chatMainModelStatus";
import type { ChatConnectionState } from "./ChatConnectionStatus";
import { ChatThreadSidebar } from "./ChatThreadSidebar";
import { ChatTranscript } from "./ChatTranscript";
import { ChatTranscriptTools } from "./ChatTranscriptTools";
import { ChatItemView, type Item } from "./ChatEventDispatcher";
import { useChatStreamController } from "./ChatStreamController";
import {
  DetailSheet,
} from "./ListDetailLayout";
import {
  FormField,
  InlineAlert,
  SelectInput,
  TextInput,
  ToolbarButton,
  ToolbarLink,
} from "./DashboardChrome";
import { Icon } from "./Icon";
import { ChatDraftProvider, useChatDraft } from "../providers/ChatDraftContext";
import { ThreadProvider, useThreadContext } from "../providers/ThreadContext";

// Re-exported for tests and downstream consumers that depended on these living
// in Chat before the module split.
export {
  appendProgressEntry,
  type ProgressEntry,
} from "./ChatEventDispatcher";

type PolicyChoice = "" | "true" | "false";

const APPROVAL_KEY = "a2a.approval_mode";
const LLM_CREDS_KEY = "a2a.llm_creds_name";
const MAIN_LLM_SOURCE_KEY = "a2a.main_llm_source";

function storedValue(key: string): string | null {
  if (typeof window === "undefined") return null;
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

function storeValue(key: string, value: string | null) {
  if (typeof window === "undefined") return;
  try {
    if (value === null) window.localStorage.removeItem(key);
    else window.localStorage.setItem(key, value);
  } catch {
    // Storage can be unavailable in hardened/private browser contexts. The
    // in-memory selection still works for the current task.
  }
}

export function chatOrganizationSlugFromList(
  organizations: readonly Pick<Organization, "slug">[],
): string | undefined {
  return organizations[0]?.slug || undefined;
}

export type ChatProps = {
  filesOpen?: boolean;
  onFilesOpenChange?: (open: boolean) => void;
  activityOpen?: boolean;
  onActivityOpenChange?: (open: boolean) => void;
  initialThreadsOpen?: boolean;
  initialSettingsOpen?: boolean;
};

/**
 * Chat — thin layout orchestrator for the workspace chat console. Composes the
 * thread list (ChatThreadSidebar), the live transcript (ChatStreamController +
 * ChatEventDispatcher), and the composer. Thread selection lives in
 * {@link ThreadProvider} and the composer draft in {@link ChatDraftProvider}.
 */
export function Chat(props: ChatProps = {}) {
  return (
    <ThreadProvider>
      <ChatDraftProvider>
        <ChatConsole {...props} />
      </ChatDraftProvider>
    </ThreadProvider>
  );
}

function boolChoice(value: boolean | undefined): PolicyChoice {
  if (value === true) return "true";
  if (value === false) return "false";
  return "";
}

function choiceValue(value: PolicyChoice): boolean | undefined {
  if (value === "true") return true;
  if (value === "false") return false;
  return undefined;
}

function sanitizedPositiveInt(value: string, max: number): number | undefined {
  const parsed = Number(value);
  if (!Number.isFinite(parsed) || parsed <= 0) return undefined;
  return Math.max(1, Math.min(max, Math.floor(parsed)));
}

function sanitizedNonNegativeInt(value: string, max: number): number | undefined {
  if (value.trim() === "") return undefined;
  const parsed = Number(value);
  if (!Number.isFinite(parsed) || parsed < 0) return undefined;
  return Math.max(0, Math.min(max, Math.floor(parsed)));
}

function ChatConsole({
  filesOpen,
  onFilesOpenChange,
  activityOpen,
  onActivityOpenChange,
  initialThreadsOpen,
  initialSettingsOpen,
}: ChatProps) {
  const {
    activeThreadId,
    threads,
    setActiveThread,
    refreshThreads,
    loadMoreThreads,
    hasMore,
    loadingMore,
    removeThread,
  } = useThreadContext();
  const { composerDraft, setComposerDraft } = useChatDraft();

  const [approvalMode] = useState<boolean>(
    () => storedValue(APPROVAL_KEY) === "1",
  );
  const [llmCredsName, setLlmCredsName] = useState<string>(
    () => storedValue(LLM_CREDS_KEY) || "",
  );
  const [mainLlmSource] = useState<MainLlmSource>(() =>
    storedValue(MAIN_LLM_SOURCE_KEY) === "platform" ? "platform" : "user",
  );
  const [runBudgetOverride, setRunBudgetOverride] = useState<string>("");
  const [maxHandoffOverride, setMaxHandoffOverride] = useState<string>("");
  const [writeApprovalOverride, setWriteApprovalOverride] = useState<PolicyChoice>("");
  const [denyNetworkOverride, setDenyNetworkOverride] = useState<PolicyChoice>("");
  const [onlyApprovedAgentsOverride, setOnlyApprovedAgentsOverride] =
    useState<PolicyChoice>("");
  const [piiSafeModeOverride, setPiiSafeModeOverride] = useState<PolicyChoice>("");
  const [approvedAgentsOverride, setApprovedAgentsOverride] = useState<string>("");
  const [credsList, setCredsList] = useState<LLMCreds[]>([]);
  const [credsLoaded, setCredsLoaded] = useState(false);
  const [workspaceHasFiles, setWorkspaceHasFiles] = useState<boolean | undefined>(
    undefined,
  );
  // The thread rail is task context, not secondary navigation. Keep it visible
  // by default on desktop so users stay oriented while inspecting work.
  const [threadsOpen, setThreadsOpen] = useState(initialThreadsOpen ?? true);
  const [mobileThreadsOpen, setMobileThreadsOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(Boolean(initialSettingsOpen));

  const chatOrganizationSlugRef = useRef<string | null>(null);

  const mainModelStatus = useMemo(
    () => resolveChatMainModelStatus(credsList, llmCredsName, credsLoaded),
    [credsList, credsLoaded, llmCredsName],
  );

  useEffect(() => {
    let cancelled = false;
    setCredsLoaded(false);
    listLlmCreds()
      .then((creds) => {
        if (!cancelled) setCredsList(creds);
      })
      .catch(() => {
        if (!cancelled) setCredsList([]);
      })
      .finally(() => {
        if (!cancelled) setCredsLoaded(true);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    listOrganizations({ purpose: "chat", limit: 1 })
      .then((organizations) => {
        if (!cancelled) {
          chatOrganizationSlugRef.current =
            chatOrganizationSlugFromList(organizations) ?? null;
        }
      })
      .catch(() => {
        if (!cancelled) chatOrganizationSlugRef.current = null;
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    let cancelled = false;
    hasWorkspaceFiles()
      .then((has) => {
        if (!cancelled) setWorkspaceHasFiles(has);
      })
      .catch(() => {
        if (!cancelled) setWorkspaceHasFiles(undefined);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  function setLlmCreds(name: string) {
    setLlmCredsName(name);
    storeValue(LLM_CREDS_KEY, name || null);
  }

  useEffect(() => {
    if (!credsLoaded || credsList.length === 0) return;
    const requestedCredentialExists = llmCredsName
      ? credsList.some((cred) => cred.name === llmCredsName)
      : credsList.some((cred) => cred.name === "default");
    if (requestedCredentialExists) return;
    const fallback = mainModelStatus.credential;
    setLlmCreds(fallback?.name === "default" ? "" : fallback?.name ?? "");
  }, [credsList, credsLoaded, llmCredsName, mainModelStatus.credential]);

  function applyThreadSettings(settings: ChatThreadSettings | undefined) {
    setRunBudgetOverride(
      settings?.run_budget_cents || settings?.run_budget_cents === 0
        ? String(settings.run_budget_cents)
        : "",
    );
    setMaxHandoffOverride(
      settings?.max_agent_calls_per_run ? String(settings.max_agent_calls_per_run) : "",
    );
    setWriteApprovalOverride(boolChoice(settings?.require_approval_for_file_writes));
    setDenyNetworkOverride(boolChoice(settings?.deny_external_network));
    setOnlyApprovedAgentsOverride(boolChoice(settings?.only_approved_agents));
    setPiiSafeModeOverride(boolChoice(settings?.pii_safe_mode));
    setApprovedAgentsOverride((settings?.approved_agents || []).join(", "));
  }

  function buildThreadSettings(): ChatThreadSettings {
    const settings: ChatThreadSettings = {};
    const runBudget = sanitizedNonNegativeInt(runBudgetOverride, 1_000_000);
    if (runBudget !== undefined) settings.run_budget_cents = runBudget;
    const maxHandoffs = sanitizedPositiveInt(maxHandoffOverride, 100);
    if (maxHandoffs !== undefined) settings.max_agent_calls_per_run = maxHandoffs;
    const writeApproval = choiceValue(writeApprovalOverride);
    if (writeApproval !== undefined) settings.require_approval_for_file_writes = writeApproval;
    const denyNetwork = choiceValue(denyNetworkOverride);
    if (denyNetwork !== undefined) settings.deny_external_network = denyNetwork;
    const onlyApprovedAgents = choiceValue(onlyApprovedAgentsOverride);
    if (onlyApprovedAgents !== undefined) settings.only_approved_agents = onlyApprovedAgents;
    const piiSafeMode = choiceValue(piiSafeModeOverride);
    if (piiSafeMode !== undefined) settings.pii_safe_mode = piiSafeMode;
    const approvedAgents = approvedAgentsOverride
      .split(",")
      .map((item) => item.trim())
      .filter(Boolean);
    if (approvedAgents.length > 0) settings.approved_agents = approvedAgents;
    return settings;
  }

  function threadPolicyOverrides(): ChatThreadSettings | undefined {
    const settings = buildThreadSettings();
    return Object.keys(settings).length > 0 ? settings : undefined;
  }

  const stream = useChatStreamController({
    threadId: activeThreadId,
    onThreadCreated: setActiveThread,
    refreshThreads: () => void refreshThreads(),
    mainModelStatus,
    approvalMode,
    mainLlmSource,
    organizationSlug: () => chatOrganizationSlugRef.current || undefined,
    threadPolicyOverrides,
    applyThreadSettings,
  });

  const {
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
  } = stream;

  const followOutput = streaming ? "smooth" : "auto";
  const canUseMainModel = mainModelStatus.canChat;
  const mainModelLabel = mainModelStatus.label;
  const composerDisabled = !canUseMainModel || threadContentLoading;
  const composerPlaceholder = threadContentLoading
    ? "Loading thread..."
    : mainModelStatus.placeholder;

  const activeThread = threads?.find((t) => t.id === activeThreadId) ?? null;
  const headerTitle =
    activeThread?.title?.trim() ||
    loadedThreadTitle ||
    (activeThreadId
      ? threadContentLoading
        ? "Loading chat"
        : "Untitled chat"
      : "New chat");

  return (
    <div className="flex h-full min-h-0 min-w-0 w-full bg-runtime-bg text-ink-soft">
      <ChatThreadSidebar
        threads={threads}
        activeId={activeThreadId}
        onPick={(id) => setActiveThread(id)}
        onNew={() => setActiveThread(null)}
        onDelete={removeThread}
        onOpenSettings={(id) => {
          setActiveThread(id);
          setSettingsOpen(true);
        }}
        onLoadMore={loadMoreThreads}
        hasMore={hasMore}
        loadingMore={loadingMore}
        desktopOpen={threadsOpen}
        onDesktopOpenChange={setThreadsOpen}
        mobileOpen={mobileThreadsOpen}
        onMobileOpenChange={setMobileThreadsOpen}
      />

      <div className="relative flex h-full min-h-0 min-w-0 flex-1 flex-col bg-runtime-bg">
        <ChatHeader
          title={headerTitle}
          mainModelLabel={mainModelLabel}
          connectionState={connectionState}
          threadContentLoading={threadContentLoading}
          approvalMode={approvalMode}
          items={items}
          threadId={activeThreadId}
          mobileThreadsOpen={mobileThreadsOpen}
          onOpenMobileThreads={() => setMobileThreadsOpen(true)}
          threadsOpen={threadsOpen}
          onToggleThreads={() => setThreadsOpen((open) => !open)}
          onOpenSettings={activeThreadId ? () => setSettingsOpen(true) : undefined}
          filesOpen={filesOpen}
          onToggleFiles={
            onFilesOpenChange ? () => onFilesOpenChange(!filesOpen) : undefined
          }
          activityOpen={activityOpen}
          onToggleActivity={
            onActivityOpenChange
              ? () => onActivityOpenChange(!activityOpen)
              : undefined
          }
        />

        <ChatTranscript
          items={items}
          followOutput={followOutput}
          streaming={streaming}
          connectionState={connectionState}
          streamStatus={streamStatus}
          error={err}
          loading={threadContentLoading}
          assistantDraft={assistantDraft}
          mainModelLabel={mainModelLabel}
          workspaceHasFiles={workspaceHasFiles}
          canUseMainModel={canUseMainModel}
          onCancel={cancel}
          onPickPrompt={(prompt) => void send(prompt)}
          renderItem={(item, streamingTail) => (
            <ChatItemView item={item} streaming={streamingTail} />
          )}
        />

        {mainModelStatus.blocker && (
          <div className="shrink-0 bg-runtime-bg px-4 pt-3">
            <InlineAlert
              tone={mainModelStatus.loading ? "neutral" : "amber"}
              className="mx-auto max-w-3xl text-xs"
            >
              <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
                <span>{mainModelStatus.blocker}</span>
                {!mainModelStatus.loading && (
                  <ToolbarLink href="/llm-keys/add" size="xs" variant="secondary">
                    <Icon name="key" size={12} />
                    Add LLM key
                  </ToolbarLink>
                )}
              </div>
            </InlineAlert>
          </div>
        )}

        <ChatComposer
          busy={streaming}
          disabled={composerDisabled}
          value={composerDraft}
          onValueChange={setComposerDraft}
          onCancel={cancel}
          onSend={send}
          placeholder={composerPlaceholder}
        />
      </div>

      <ThreadSettingsSheet
        open={settingsOpen && Boolean(activeThreadId)}
        threadId={activeThreadId}
        threadTitle={headerTitle}
        runBudget={runBudgetOverride}
        maxHandoffs={maxHandoffOverride}
        writeApproval={writeApprovalOverride}
        denyNetwork={denyNetworkOverride}
        onlyApprovedAgents={onlyApprovedAgentsOverride}
        piiSafeMode={piiSafeModeOverride}
        approvedAgents={approvedAgentsOverride}
        onClose={() => setSettingsOpen(false)}
        onChange={{
          runBudget: setRunBudgetOverride,
          maxHandoffs: setMaxHandoffOverride,
          writeApproval: setWriteApprovalOverride,
          denyNetwork: setDenyNetworkOverride,
          onlyApprovedAgents: setOnlyApprovedAgentsOverride,
          piiSafeMode: setPiiSafeModeOverride,
          approvedAgents: setApprovedAgentsOverride,
        }}
        buildSettings={buildThreadSettings}
        onSaved={() => void refreshThreads()}
      />
    </div>
  );
}

function ChatHeader({
  title,
  mainModelLabel,
  connectionState,
  threadContentLoading,
  approvalMode,
  items,
  threadId,
  mobileThreadsOpen,
  onOpenMobileThreads,
  threadsOpen,
  onToggleThreads,
  onOpenSettings,
  filesOpen,
  onToggleFiles,
  activityOpen,
  onToggleActivity,
}: {
  title: string;
  mainModelLabel: string;
  connectionState: ChatConnectionState;
  threadContentLoading: boolean;
  approvalMode: boolean;
  items: Item[];
  threadId: string | null;
  mobileThreadsOpen: boolean;
  onOpenMobileThreads: () => void;
  threadsOpen: boolean;
  onToggleThreads: () => void;
  onOpenSettings?: () => void;
  filesOpen?: boolean;
  onToggleFiles?: () => void;
  activityOpen?: boolean;
  onToggleActivity?: () => void;
}) {
  return (
    <header className="flex min-h-14 shrink-0 items-center justify-between gap-2 border-b border-runtime-line bg-runtime-panel/80 px-3 py-2 sm:px-4">
      <div className="flex min-w-0 flex-1 items-center gap-2.5">
        <span className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border border-signal-protocol/35 bg-signal-protocol/10 text-signal-protocol shadow-sm shadow-black/20">
          <Icon name="sparkle" size={13} />
        </span>
        <div className="min-w-0">
          <div className="font-mono text-[9px] uppercase tracking-[0.16em] text-ink-faint">
            current task
          </div>
          <div className="flex min-w-0 items-center gap-2">
            <span className="min-w-0 truncate text-sm font-semibold text-ink">
              {title}
            </span>
            {connectionState === "streaming" && (
              <span className="inline-flex items-center gap-1 rounded-full border border-signal-live/45 bg-signal-live/12 px-1.5 py-0.5 font-mono text-[10px] uppercase text-signal-live">
                <span className="h-1 w-1 animate-pulse rounded-full bg-signal-live" />
                streaming
              </span>
            )}
            {connectionState === "waiting" && (
              <span className="inline-flex items-center gap-1 rounded-full border border-signal-authority/45 bg-signal-authority/12 px-1.5 py-0.5 font-mono text-[10px] uppercase text-signal-authority">
                <span className="h-1 w-1 animate-pulse rounded-full bg-signal-authority" />
                waiting
              </span>
            )}
            {threadContentLoading && connectionState === "idle" && (
              <span className="inline-flex items-center gap-1 rounded-full border border-runtime-line-soft/70 bg-runtime-panel/70 px-1.5 py-0.5 font-mono text-[10px] uppercase text-ink-dim">
                <span className="h-1 w-1 animate-pulse rounded-full bg-signal-authority" />
                loading
              </span>
            )}
            {approvalMode && (
              <span
                title="Approve handoffs is on"
                className="inline-flex items-center gap-1 rounded-full border border-signal-authority/45 bg-signal-authority/12 px-1.5 py-0.5 font-mono text-[10px] uppercase text-signal-authority/90"
              >
                <Icon name="shield" size={10} />
                approvals
              </span>
            )}
          </div>
          <div className="truncate font-mono text-[10px] text-ink-faint">
            main agent · {mainModelLabel} · context stays open
          </div>
        </div>
      </div>

      <div className="flex shrink-0 items-center gap-1.5">
        <TranscriptActionsMenu
          items={items}
          title={threadId ? `Chat ${threadId.slice(0, 8)}` : "New chat"}
          filename={threadId ? `a2a-chat-${threadId.slice(0, 8)}` : "a2a-chat"}
        />
        {onToggleFiles && (
          <ToolbarButton
            onClick={onToggleFiles}
            active={filesOpen}
            aria-label={filesOpen ? "Hide workspace files" : "Show workspace files"}
            aria-pressed={filesOpen}
            title={filesOpen ? "Hide workspace files" : "Show workspace files"}
            variant={filesOpen ? "primary" : "ghost"}
            size="md"
            className="inline-flex h-8 w-8 gap-1.5 px-0 lg:w-auto lg:px-2"
          >
            <Icon name={filesOpen ? "folder-open" : "folder"} size={14} />
            <span className="hidden xl:inline">Files</span>
          </ToolbarButton>
        )}
        {onToggleActivity && (
          <ToolbarButton
            onClick={onToggleActivity}
            active={activityOpen}
            aria-label={activityOpen ? "Hide thread activity" : "Show thread activity"}
            aria-pressed={activityOpen}
            title={activityOpen ? "Hide thread activity" : "Show thread activity"}
            variant={activityOpen ? "primary" : "ghost"}
            size="md"
            className="inline-flex h-8 w-8 gap-1.5 px-0 lg:w-auto lg:px-2"
          >
            <Icon name="panel-right" size={14} />
            <span className="hidden xl:inline">Activity</span>
          </ToolbarButton>
        )}
        {onOpenSettings && (
          <ToolbarButton
            onClick={onOpenSettings}
            aria-label="Thread settings"
            title="Thread settings"
            variant="ghost"
            size="md"
            className="h-8 w-8 px-0"
          >
            <Icon name="sliders" size={14} />
          </ToolbarButton>
        )}
        <ToolbarButton
          onClick={onOpenMobileThreads}
          active={mobileThreadsOpen}
          aria-label="Open chat threads"
          aria-controls="chat-thread-mobile-drawer"
          aria-expanded={mobileThreadsOpen}
          title="Open chat threads"
          variant="ghost"
          size="md"
          className="h-8 w-8 px-0 md:hidden"
        >
          <Icon name="panel-left" size={14} />
        </ToolbarButton>
        <ToolbarButton
          onClick={onToggleThreads}
          active={threadsOpen}
          aria-label={threadsOpen ? "Hide thread list" : "Show thread list"}
          title={threadsOpen ? "Hide thread list" : "Show thread list"}
          variant={threadsOpen ? "primary" : "ghost"}
          size="md"
          className="hidden h-8 gap-1.5 px-2 md:inline-flex"
        >
          <Icon name="panel-left" size={14} />
          <span className="hidden xl:inline">Threads</span>
        </ToolbarButton>
      </div>
    </header>
  );
}

function TranscriptActionsMenu({
  items,
  title,
  filename,
}: {
  items: Item[];
  title: string;
  filename: string;
}) {
  return (
    <details className="group relative">
      <summary
        className="flex h-8 w-8 cursor-pointer list-none items-center justify-center rounded-md border border-transparent text-ink-muted transition hover:border-runtime-line-soft hover:bg-runtime-raised hover:text-ink focus:outline-none focus-visible:ring-2 focus-visible:ring-signal-protocol/40 [&::-webkit-details-marker]:hidden"
        aria-label="Transcript actions"
        title="Transcript actions"
      >
        <Icon name="more" size={15} />
      </summary>
      <div className="absolute right-0 top-10 z-30 w-44 rounded-lg border border-runtime-line bg-runtime-raised p-2 shadow-2xl shadow-black/60">
        <div className="mb-1 px-1 font-mono text-[9px] uppercase tracking-[0.14em] text-ink-faint">
          transcript
        </div>
        <ChatTranscriptTools
          items={items}
          title={title}
          filename={filename}
          className="flex-col items-stretch [&>button]:w-full [&>button]:justify-start"
        />
      </div>
    </details>
  );
}

type PolicyChoiceSetters = {
  runBudget: (v: string) => void;
  maxHandoffs: (v: string) => void;
  writeApproval: (v: PolicyChoice) => void;
  denyNetwork: (v: PolicyChoice) => void;
  onlyApprovedAgents: (v: PolicyChoice) => void;
  piiSafeMode: (v: PolicyChoice) => void;
  approvedAgents: (v: string) => void;
};

/**
 * ThreadSettingsSheet — mandate-C in-place settings surface. Thread-scoped
 * policy opens as a right-side {@link DetailSheet} instead of navigating to the
 * full /workspace/settings route, so the transcript and list stay mounted.
 */
function ThreadSettingsSheet({
  open,
  threadId,
  threadTitle,
  runBudget,
  maxHandoffs,
  writeApproval,
  denyNetwork,
  onlyApprovedAgents,
  piiSafeMode,
  approvedAgents,
  onClose,
  onChange,
  buildSettings,
  onSaved,
}: {
  open: boolean;
  threadId: string | null;
  threadTitle: string;
  runBudget: string;
  maxHandoffs: string;
  writeApproval: PolicyChoice;
  denyNetwork: PolicyChoice;
  onlyApprovedAgents: PolicyChoice;
  piiSafeMode: PolicyChoice;
  approvedAgents: string;
  onClose: () => void;
  onChange: PolicyChoiceSetters;
  buildSettings: () => ChatThreadSettings;
  onSaved: () => void;
}) {
  const [saving, setSaving] = useState(false);
  const [saveErr, setSaveErr] = useState<string | null>(null);

  async function save() {
    if (!threadId || saving) return;
    setSaving(true);
    setSaveErr(null);
    try {
      await updateThreadSettings(threadId, buildSettings());
      onSaved();
      onClose();
    } catch (ex) {
      setSaveErr(ex instanceof Error ? ex.message : String(ex));
    } finally {
      setSaving(false);
    }
  }

  return (
    <DetailSheet
      open={open}
      onClose={onClose}
      title="Thread settings"
      description={
        <span className="truncate font-mono text-[11px] text-ink-faint">
          {threadId ? threadTitle : "no thread"}
        </span>
      }
      footer={
        <div className="flex justify-end gap-2">
          <ToolbarButton onClick={onClose} disabled={saving} size="sm">
            Cancel
          </ToolbarButton>
          <ToolbarButton onClick={save} variant="primary" size="sm" disabled={saving}>
            {saving ? "Saving..." : "Save settings"}
          </ToolbarButton>
        </div>
      }
    >
      <div className="space-y-4">
        {saveErr && (
          <InlineAlert tone="red" role="alert" className="text-xs">
            {saveErr}
          </InlineAlert>
        )}
        <div className="grid gap-3 sm:grid-cols-2">
          <FormField label="Run budget (cents)">
            <TextInput
              type="number"
              inputMode="numeric"
              value={runBudget}
              onChange={(e) => onChange.runBudget(e.target.value)}
              placeholder="Inherit account"
            />
          </FormField>
          <FormField label="Max handoffs per run">
            <TextInput
              type="number"
              inputMode="numeric"
              value={maxHandoffs}
              onChange={(e) => onChange.maxHandoffs(e.target.value)}
              placeholder="Inherit account"
            />
          </FormField>
          <FormField label="Require approval for file writes">
            <PolicySelect
              value={writeApproval}
              onChange={onChange.writeApproval}
            />
          </FormField>
          <FormField label="Deny external network">
            <PolicySelect value={denyNetwork} onChange={onChange.denyNetwork} />
          </FormField>
          <FormField label="PII safe mode">
            <PolicySelect value={piiSafeMode} onChange={onChange.piiSafeMode} />
          </FormField>
          <FormField label="Only approved agents">
            <PolicySelect
              value={onlyApprovedAgents}
              onChange={onChange.onlyApprovedAgents}
            />
          </FormField>
        </div>
        <FormField
          label="Approved agents"
          description="Comma-separated agent names used when allowlist-only routing is on."
        >
          <TextInput
            value={approvedAgents}
            onChange={(e) => onChange.approvedAgents(e.target.value)}
            placeholder="agent-a, agent-b"
          />
        </FormField>
        <div className="flex justify-end border-t border-runtime-line-soft/60 pt-3">
          <ToolbarButton
            size="xs"
            variant="ghost"
            onClick={() => {
              onChange.runBudget("");
              onChange.maxHandoffs("");
              onChange.writeApproval("");
              onChange.denyNetwork("");
              onChange.onlyApprovedAgents("");
              onChange.piiSafeMode("");
              onChange.approvedAgents("");
            }}
          >
            Reset to account policy
          </ToolbarButton>
        </div>
      </div>
    </DetailSheet>
  );
}

function PolicySelect({
  value,
  onChange,
}: {
  value: PolicyChoice;
  onChange: (next: PolicyChoice) => void;
}) {
  return (
    <SelectInput
      value={value}
      onChange={(e) => onChange(e.target.value as PolicyChoice)}
    >
      <option value="">Inherit</option>
      <option value="true">On</option>
      <option value="false">Off</option>
    </SelectInput>
  );
}
