import { useCallback, useEffect, useState } from "react";
import {
  createAgentMailbox,
  deleteAgentMailbox,
  getAgentMailbox,
  updateAgentMailbox,
  type AgentMailbox,
  type MyAgentListing,
} from "../api";
import {
  CopyValueRow,
  InlineAlert,
  LoadingState,
  ProgressBar,
  SectionPanel,
  SummaryMetric,
  TextInput,
  ToolbarButton,
  type StatusBadgeTone,
} from "./DashboardChrome";
import { StatusBadge } from "./StatusPillAdapters";

export function AgentMailboxPanel({ agent }: { agent: MyAgentListing }) {
  const [mailbox, setMailbox] = useState<AgentMailbox | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [sender, setSender] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [ok, setOk] = useState<string | null>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);

  const load = useCallback(async () => {
    try {
      const nextMailbox = await getAgentMailbox(agent.name);
      setMailbox(nextMailbox);
      setErr(null);
    } catch (ex) {
      setErr(ex instanceof Error ? ex.message : String(ex));
    } finally {
      setLoaded(true);
    }
  }, [agent.name]);

  useEffect(() => {
    void load();
  }, [load]);

  async function enableInbox() {
    if (busy) return;
    setBusy("create");
    setErr(null);
    setOk(null);
    try {
      const next = await createAgentMailbox(agent.name);
      setMailbox(next);
      setOk(`${next.address} requested. Provisioning usually finishes in a minute.`);
    } catch (ex) {
      {
        setErr(ex instanceof Error ? ex.message : String(ex));
      }
    } finally {
      setBusy(null);
    }
  }

  async function saveAllowedSenders(nextSenders: string[], message: string) {
    setBusy("allowlist");
    setErr(null);
    setOk(null);
    try {
      const next = await updateAgentMailbox(agent.name, {
        allowed_senders: nextSenders,
      });
      setMailbox(next);
      setOk(message);
      return true;
    } catch (ex) {
      setErr(ex instanceof Error ? ex.message : String(ex));
      return false;
    } finally {
      setBusy(null);
    }
  }

  async function addSender() {
    if (!mailbox || busy) return;
    const next = normalizeSenderAddress(sender);
    if (!next) return;
    if (!isPlausibleEmail(next)) {
      setErr(`"${next}" does not look like an email address.`);
      return;
    }
    if (mailbox.allowed_senders.includes(next)) {
      setErr(`${next} is already on the allowlist.`);
      return;
    }
    const saved = await saveAllowedSenders(
      [...mailbox.allowed_senders, next],
      `${next} added to the allowlist.`,
    );
    if (saved) setSender("");
  }

  async function removeSender(address: string) {
    if (!mailbox || busy) return;
    await saveAllowedSenders(
      mailbox.allowed_senders.filter((existing) => existing !== address),
      `${address} removed from the allowlist.`,
    );
  }

  async function removeInbox() {
    if (!mailbox || busy) return;
    setBusy("delete");
    setErr(null);
    setOk(null);
    try {
      await deleteAgentMailbox(agent.name);
      setMailbox(null);
      setConfirmDelete(false);
      setOk("Email inbox removed.");
    } catch (ex) {
      setErr(ex instanceof Error ? ex.message : String(ex));
    } finally {
      setBusy(null);
    }
  }

  async function refresh() {
    if (busy) return;
    setBusy("refresh");
    try {
      await load();
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="mt-4 grid gap-4 border-t border-runtime-line-soft/60 pt-4">
      {!loaded ? (
        <LoadingState label="Loading email inbox..." />
      ) : (
        <>
          {err && (
            <InlineAlert tone="red" className="text-xs">{err}</InlineAlert>
          )}
          {ok && (
            <InlineAlert tone="emerald" className="text-xs">{ok}</InlineAlert>
          )}

          {mailbox === null ? (
            <SectionPanel
              title="Email inbox"
              description="Give this agent its own email address. Inbound mail from allowed senders opens a chat thread, and the agent replies by email."
            >
              <div className="flex flex-wrap items-center gap-2">
                <ToolbarButton
                  onClick={() => void enableInbox()}
                  disabled={Boolean(busy)}
                  variant="primary"
                  size="md"
                >
                  {busy === "create" ? "Enabling..." : "Enable inbox"}
                </ToolbarButton>
              </div>

            </SectionPanel>
          ) : (
            <>
              <SectionPanel
                title="Email inbox"
                description="Inbound mail from allowed senders opens a chat thread, and the agent replies by email."
              >
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <StatusBadge tone={mailboxStatusTone(mailbox.status)}>
                    {mailbox.status}
                  </StatusBadge>
                  <div className="flex flex-wrap gap-2">
                    {isPendingMailboxStatus(mailbox.status) && (
                      <ToolbarButton
                        onClick={() => void refresh()}
                        disabled={Boolean(busy)}
                        size="xs"
                      >
                        {busy === "refresh" ? "Refreshing..." : "Refresh status"}
                      </ToolbarButton>
                    )}
                    <ToolbarButton
                      onClick={() => {
                        if (!confirmDelete) {
                          setConfirmDelete(true);
                          return;
                        }
                        void removeInbox();
                      }}
                      disabled={Boolean(busy)}
                      variant="danger"
                      size="xs"
                    >
                      {busy === "delete"
                        ? "Removing..."
                        : confirmDelete
                          ? "Confirm remove"
                          : "Remove inbox"}
                    </ToolbarButton>
                    {confirmDelete && (
                      <ToolbarButton
                        onClick={() => setConfirmDelete(false)}
                        disabled={Boolean(busy)}
                        size="xs"
                      >
                        Cancel
                      </ToolbarButton>
                    )}
                  </div>
                </div>

                {confirmDelete && (
                  <InlineAlert tone="amber" className="mt-3 text-xs">
                    Removing the inbox stops all inbound and outbound email for{" "}
                    {mailbox.address}. This cannot be undone.
                  </InlineAlert>
                )}
                {mailbox.status === "failed" && (
                  <InlineAlert tone="red" className="mt-3 text-xs">
                    Provisioning failed. Remove the inbox and enable it again, or
                    contact support if it keeps failing.
                  </InlineAlert>
                )}
                {mailbox.status === "disabled" && (
                  <InlineAlert tone="amber" className="mt-3 text-xs">
                    This inbox is disabled
                    {mailbox.disabled_at
                      ? ` since ${formatMailboxDate(mailbox.disabled_at)}`
                      : ""}
                    . Inbound mail is rejected.
                  </InlineAlert>
                )}

                <div className="mt-3">
                  <CopyValueRow label="address" value={mailbox.address} />
                </div>

                <div className="mt-3 grid gap-2 md:grid-cols-2 xl:grid-cols-4">
                  <SummaryMetric
                    label="daily sends"
                    value={mailboxSendUsage(mailbox).text}
                    size="compact"
                  >
                    {mailboxSendUsage(mailbox).percent !== null && (
                      <ProgressBar
                        value={mailboxSendUsage(mailbox).percent ?? 0}
                        tone={mailboxSendUsageTone(mailbox)}
                        className="mt-2"
                        aria-label="Daily send usage"
                      />
                    )}
                  </SummaryMetric>
                  <SummaryMetric
                    label="quota"
                    value={formatQuotaBytes(mailbox.quota_bytes)}
                    size="compact"
                  />
                  <SummaryMetric
                    label="created"
                    value={formatMailboxDate(mailbox.created_at)}
                    size="compact"
                    mono={false}
                  />
                  <SummaryMetric
                    label="updated"
                    value={formatMailboxDate(mailbox.updated_at)}
                    size="compact"
                    mono={false}
                  />
                </div>
              </SectionPanel>

              <SectionPanel
                title="Sender allowlist"
                description="Only these addresses can start or continue email threads. An empty allowlist rejects all inbound mail."
              >
                <div className="grid gap-2 lg:grid-cols-[minmax(240px,1fr)_auto]">
                  <TextInput
                    value={sender}
                    onChange={(e) => setSender(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") void addSender();
                    }}
                    disabled={Boolean(busy)}
                    placeholder="person@example.com"
                    aria-label="Allowed sender email"
                    mono
                  />
                  <ToolbarButton
                    onClick={() => void addSender()}
                    disabled={!normalizeSenderAddress(sender) || Boolean(busy)}
                    variant="primary"
                    size="md"
                  >
                    {busy === "allowlist" ? "Saving..." : "Add sender"}
                  </ToolbarButton>
                </div>

                {mailbox.allowed_senders.length === 0 ? (
                  <InlineAlert tone="neutral" className="mt-3 text-xs">
                    No allowed senders yet. Add an address to accept inbound mail.
                  </InlineAlert>
                ) : (
                  <div className="mt-3 flex flex-wrap gap-1.5">
                    {mailbox.allowed_senders.map((address) => (
                      <span
                        key={address}
                        className="flex max-w-full items-center gap-1.5 rounded-md border border-runtime-line-soft/60 bg-runtime-panel/70 px-2 py-1 font-mono text-xs text-ink-dim"
                      >
                        <span className="truncate">{address}</span>
                        <button
                          type="button"
                          onClick={() => void removeSender(address)}
                          disabled={Boolean(busy)}
                          aria-label={`Remove ${address} from allowlist`}
                          className="shrink-0 text-ink-faint transition hover:text-ink disabled:opacity-50"
                        >
                          ×
                        </button>
                      </span>
                    ))}
                  </div>
                )}
              </SectionPanel>
            </>
          )}
        </>
      )}
    </div>
  );
}


function mailboxStatusTone(status: string): StatusBadgeTone {
  if (status === "ready") return "emerald";
  if (status === "disabled" || status === "failed") return "red";
  return "amber";
}

function isPendingMailboxStatus(status: string) {
  return status === "pending" || status === "provisioning";
}

function mailboxSendUsage(mailbox: AgentMailbox): {
  text: string;
  percent: number | null;
} {
  const limit = mailbox.daily_send_limit;
  if (limit === null || limit <= 0) {
    return {
      text: `${mailbox.outbound_count.toLocaleString()} / unlimited`,
      percent: null,
    };
  }
  return {
    text: `${mailbox.outbound_count.toLocaleString()} / ${limit.toLocaleString()}`,
    percent: Math.min(100, Math.round((mailbox.outbound_count / limit) * 100)),
  };
}

function mailboxSendUsageTone(mailbox: AgentMailbox): StatusBadgeTone {
  const percent = mailboxSendUsage(mailbox).percent;
  if (percent === null) return "neutral";
  if (percent >= 100) return "red";
  if (percent >= 80) return "amber";
  return "emerald";
}

function formatQuotaBytes(value: number | null): string {
  if (value === null || value <= 0) return "unlimited";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let size = value;
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  const rounded = size >= 10 || Number.isInteger(size) ? Math.round(size) : size.toFixed(1);
  return `${rounded} ${units[unit]}`;
}

function formatMailboxDate(value: string | null): string {
  if (!value) return "never";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
}

function normalizeSenderAddress(value: string): string {
  return value.trim().toLowerCase();
}

function isPlausibleEmail(value: string): boolean {
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value);
}
