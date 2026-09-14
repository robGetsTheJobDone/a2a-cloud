import { useCallback, useEffect, useMemo, useState } from "react";
import {
  addAgentDomain,
  deleteAgentDomain,
  listAgentDomains,
  verifyAgentDomain,
  type AgentCustomDomain,
  type MyAgentListing,
} from "../api";
import {
  CopyValueRow,
  EmptyState,
  InlineAlert,
  LoadingState,
  SectionPanel,
  SegmentedButton,
  SegmentedControl,
  SelectableSurfaceLink,
  SummaryMetric,
  SurfacePanel,
  TextInput,
  ToolbarButton,
  ToolbarLink,
  type StatusBadgeTone,
} from "./DashboardChrome";
import { StatusBadge } from "./StatusPillAdapters";

function agentDomainsRoute(agentName: string, search = "") {
  return `/my-agents/${encodeURIComponent(agentName)}/domains${search}`;
}

function agentDomainRoute(agentName: string, hostname: string, search = "") {
  return `${agentDomainsRoute(agentName)}/${encodeURIComponent(hostname)}${search}`;
}

export function AgentDomainsPanel({
  agent,
  fullPage = false,
  selectedHostname = null,
  search = "",
}: {
  agent: MyAgentListing;
  fullPage?: boolean;
  selectedHostname?: string | null;
  search?: string;
}) {
  const [rows, setRows] = useState<AgentCustomDomain[] | null>(null);
  const [hostname, setHostname] = useState("");
  const [includeWww, setIncludeWww] = useState(true);
  const [canonicalMode, setCanonicalMode] = useState<"apex" | "www">("apex");
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [ok, setOk] = useState<string | null>(null);
  const hosted = !agent.image.startsWith("external-a2a:");
  const enabled = agent.public && hosted;
  const normalizedHost = normalizeHostname(hostname);
  const canonicalHostname = useMemo(() => {
    if (!normalizedHost) return undefined;
    if (!includeWww) return normalizedHost;
    if (canonicalMode === "www") {
      return normalizedHost.startsWith("www.") ? normalizedHost : `www.${normalizedHost}`;
    }
    return normalizedHost.startsWith("www.") ? normalizedHost.slice(4) : normalizedHost;
  }, [canonicalMode, includeWww, normalizedHost]);
  const selectedDomain = useMemo(
    () =>
      selectedHostname && rows
        ? rows.find((row) => row.hostname === selectedHostname) || null
        : null,
    [rows, selectedHostname],
  );

  const load = useCallback(async () => {
    try {
      const domains = await listAgentDomains(agent.name);
      setRows(domains);
      setErr(null);
    } catch (ex) {
      setErr(ex instanceof Error ? ex.message : String(ex));
    }
  }, [agent.name]);

  useEffect(() => {
    void load();
  }, [load]);

  async function addDomain() {
    const next = normalizedHost;
    if (!next || busy || !enabled) return;
    setBusy("add");
    setErr(null);
    setOk(null);
    try {
      const row = await addAgentDomain(agent.name, next, {
        include_www: includeWww,
        canonical_hostname: canonicalHostname,
      });
      setRows((cur) => mergeAgentDomainRows(cur || [], row));
      await load();
      setHostname("");
      setOk(includeWww ? `${next} and its www pair were added.` : `${row.hostname} added.`);
    } catch (ex) {
      setErr(ex instanceof Error ? ex.message : String(ex));
    } finally {
      setBusy(null);
    }
  }

  async function verifyDomain(host: string) {
    setBusy(`verify:${host}`);
    setErr(null);
    setOk(null);
    try {
      const row = await verifyAgentDomain(agent.name, host);
      setRows((cur) => mergeAgentDomainRows(cur || [], row));
      setOk(`${row.hostname} is active.`);
    } catch (ex) {
      setErr(ex instanceof Error ? ex.message : String(ex));
      await load();
    } finally {
      setBusy(null);
    }
  }

  async function removeDomain(host: string): Promise<boolean> {
    setBusy(`delete:${host}`);
    setErr(null);
    setOk(null);
    try {
      await deleteAgentDomain(agent.name, host);
      setRows((cur) => (cur || []).filter((row) => row.hostname !== host));
      setOk(`${host} removed.`);
      return true;
    } catch (ex) {
      setErr(ex instanceof Error ? ex.message : String(ex));
      return false;
    } finally {
      setBusy(null);
    }
  }

  const detailMode = Boolean(selectedHostname);

  return (
    <div className={fullPage ? "grid gap-4" : "mt-4 border-t border-runtime-line-soft/60 pt-4"}>
      {!detailMode && (
        <SectionPanel
          title="Domain setup"
          description="Add a hostname, create the DNS records, then verify."
        >
          <div className="grid gap-2 lg:grid-cols-[minmax(240px,1fr)_auto]">
            <TextInput
              value={hostname}
              onChange={(e) => setHostname(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") void addDomain();
              }}
              disabled={!enabled || Boolean(busy)}
              placeholder="example.com"
              aria-label="Domain hostname"
              mono
            />
            <ToolbarButton
              onClick={() => void addDomain()}
              disabled={!enabled || !normalizedHost || Boolean(busy)}
              variant="primary"
              size="md"
            >
              {busy === "add" ? "Adding..." : "Add domain"}
            </ToolbarButton>
          </div>

          <div className="mt-3 grid gap-2 md:grid-cols-2">
            <label className="flex items-center gap-2 rounded-md border border-runtime-line-soft/60 bg-runtime-panel/50 px-3 py-2 text-xs text-ink-soft">
              <input
                type="checkbox"
                checked={includeWww}
                onChange={(event) => setIncludeWww(event.target.checked)}
                disabled={!enabled || Boolean(busy)}
                className="h-4 w-4 accent-ink-muted"
              />
              Add matching www hostname
            </label>
            <SegmentedControl
              aria-label="Canonical hostname"
              className="grid grid-cols-2"
            >
              <SegmentedButton
                type="button"
                onClick={() => setCanonicalMode("apex")}
                disabled={!includeWww}
                selected={canonicalMode === "apex"}
                className="text-xs"
              >
                apex canonical
              </SegmentedButton>
              <SegmentedButton
                type="button"
                onClick={() => setCanonicalMode("www")}
                disabled={!includeWww}
                selected={canonicalMode === "www"}
                className="text-xs"
              >
                www canonical
              </SegmentedButton>
            </SegmentedControl>
          </div>

          {!enabled && (
            <InlineAlert tone="amber" className="mt-3 text-xs">
              Custom domains require a public hosted agent.
            </InlineAlert>
          )}
        </SectionPanel>
      )}

      {err && (
        <InlineAlert tone="red" className="text-xs">{err}</InlineAlert>
      )}
      {ok && (
        <InlineAlert tone="emerald" className="text-xs">{ok}</InlineAlert>
      )}

      {rows === null ? (
        <LoadingState label="Loading domains..." />
      ) : detailMode ? (
        <div className="grid gap-3 xl:grid-cols-[minmax(260px,360px)_minmax(0,1fr)]">
          <div className="grid content-start gap-2">
            <div className="text-[10px] uppercase text-ink-faint">
              Domains
            </div>
            {rows.length === 0 ? (
              <EmptyState title="No custom domains" />
            ) : (
              rows.map((row) => (
                <AgentDomainRow
                  key={row.hostname}
                  agentName={agent.name}
                  domain={row}
                  selected={row.hostname === selectedHostname}
                  search={search}
                  compact
                />
              ))
            )}
          </div>
          <AgentDomainDetail
            agent={agent}
            domain={selectedDomain}
            selectedHostname={selectedHostname || ""}
            busy={busy}
            onVerify={verifyDomain}
            onDelete={removeDomain}
            search={search}
          />
        </div>
      ) : rows.length === 0 ? (
        <EmptyState title="No custom domains" />
      ) : (
        <div className="grid gap-2">
          {rows.map((row) => (
            <AgentDomainRow
              key={row.hostname}
              agentName={agent.name}
              domain={row}
              search={search}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function AgentDomainRow({
  agentName,
  domain,
  selected = false,
  compact = false,
  search,
}: {
  agentName: string;
  domain: AgentCustomDomain;
  selected?: boolean;
  compact?: boolean;
  search: string;
}) {
  return (
    <SelectableSurfaceLink
      href={agentDomainRoute(agentName, domain.hostname, search)}
      selected={selected}
      className="p-3 text-xs"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="min-w-0">
          <div className="truncate font-mono text-ink">{domain.hostname}</div>
          <div className="mt-1 flex flex-wrap gap-2 text-[11px] text-ink-muted">
            <span>cert {domain.certificate_status}</span>
            <span>{domainRoutingLabel(domain)}</span>
            {domain.paired_www_hostname && <span>pair {domain.paired_www_hostname}</span>}
          </div>
          {!compact && domain.url && (
            <div className="mt-1 truncate font-mono text-[11px] text-ink-soft">
              {domain.url}
            </div>
          )}
        </div>
        <StatusBadge tone={domainStatusTone(domain.status)}>
          {domain.status}
        </StatusBadge>
      </div>
      <div className="mt-2 flex flex-wrap gap-2 text-[11px] text-ink-muted">
        <span>verified {formatDomainDate(domain.verified_at)}</span>
        <span>updated {formatDomainDate(domain.updated_at)}</span>
        {domain.redirect_target_url && <span>redirect</span>}
        <span className="text-ink-soft">Open</span>
      </div>
    </SelectableSurfaceLink>
  );
}

function AgentDomainDetail({
  agent,
  domain,
  selectedHostname,
  busy,
  onVerify,
  onDelete,
  search,
}: {
  agent: MyAgentListing;
  domain: AgentCustomDomain | null;
  selectedHostname: string;
  busy: string | null;
  onVerify: (hostname: string) => Promise<void>;
  onDelete: (hostname: string) => Promise<boolean>;
  search: string;
}) {
  const [confirmDelete, setConfirmDelete] = useState(false);

  useEffect(() => {
    setConfirmDelete(false);
  }, [domain?.hostname]);

  if (!domain) {
    return (
      <SurfacePanel as="section" className="min-w-0 bg-runtime-bg p-4">
        <div className="text-[10px] uppercase text-ink-faint">
          Domain detail
        </div>
        <h3 className="mt-1 font-mono text-sm text-ink [overflow-wrap:anywhere]">
          {selectedHostname}
        </h3>
        <InlineAlert tone="amber" className="mt-3 text-xs">
          This hostname is not in the current custom domain list.
        </InlineAlert>
        <ToolbarLink href={agentDomainsRoute(agent.name, search)} className="mt-3">
          All domains
        </ToolbarLink>
      </SurfacePanel>
    );
  }

  const verifying = busy === `verify:${domain.hostname}`;
  const deleting = busy === `delete:${domain.hostname}`;
  const routingLabel = domainRoutingLabel(domain);

  return (
    <SurfacePanel as="section" className="min-w-0 bg-runtime-bg">
      <div className="flex flex-col gap-3 border-b border-runtime-line-soft/60 p-4 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0">
          <div className="flex flex-wrap gap-2">
            <StatusBadge tone={domainStatusTone(domain.status)}>
              {domain.status}
            </StatusBadge>
            <StatusBadge tone={domain.certificate_status === "active" ? "emerald" : "amber"}>
              cert {domain.certificate_status}
            </StatusBadge>
          </div>
          <h3 className="mt-2 font-mono text-base text-ink [overflow-wrap:anywhere]">
            {domain.hostname}
          </h3>
          {domain.url && (
            <a
              href={domain.url}
              target="_blank"
              rel="noreferrer"
              className="mt-1 block font-mono text-xs text-ink-soft [overflow-wrap:anywhere] hover:text-ink"
            >
              {domain.url}
            </a>
          )}
        </div>
        <div className="flex flex-wrap gap-2">
          <ToolbarLink href={agentDomainsRoute(agent.name, search)}>
            All domains
          </ToolbarLink>
          {domain.status !== "active" && (
            <ToolbarButton
              onClick={() => void onVerify(domain.hostname)}
              disabled={Boolean(busy)}
            >
              {verifying ? "Verifying..." : "Verify"}
            </ToolbarButton>
          )}
          <ToolbarButton
            onClick={() => {
              if (!confirmDelete) {
                setConfirmDelete(true);
                return;
              }
              void onDelete(domain.hostname).then((success) => {
                if (success) setConfirmDelete(false);
              });
            }}
            disabled={Boolean(busy)}
            variant="danger"
          >
            {deleting ? "Removing..." : confirmDelete ? "Confirm remove" : "Remove"}
          </ToolbarButton>
          {confirmDelete && (
            <ToolbarButton
              onClick={() => setConfirmDelete(false)}
              disabled={Boolean(busy)}
            >
              Cancel
            </ToolbarButton>
          )}
        </div>
      </div>

      <div className="grid gap-4 p-4">
        {confirmDelete && (
          <InlineAlert tone="amber" className="text-xs">
            Removing this hostname stops routing traffic for the custom domain.
          </InlineAlert>
        )}
        {domain.last_error && (
          <InlineAlert tone="amber" className="text-xs">{domain.last_error}</InlineAlert>
        )}
        {domain.is_apex && !domain.routing_fallback_record_type && (
          <InlineAlert tone="neutral" className="text-xs">
            Apex domains often need ALIAS, ANAME, flattened CNAME, or A-record fallback support in the DNS provider.
          </InlineAlert>
        )}

        <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-4">
          <SummaryMetric label="dns status" value={domain.status} size="compact" />
          <SummaryMetric
            label="certificate"
            value={domain.certificate_status}
            size="compact"
          />
          <SummaryMetric
            label="verified"
            value={formatDomainDate(domain.verified_at)}
            size="compact"
            mono={false}
          />
          <SummaryMetric
            label="updated"
            value={formatDomainDate(domain.updated_at)}
            size="compact"
            mono={false}
          />
          <SummaryMetric label="setup" value={domain.dns_setup_kind} size="compact" />
          <SummaryMetric
            label="canonical"
            value={domain.canonical_hostname || "none"}
            size="compact"
          />
          <SummaryMetric
            label="paired www"
            value={domain.paired_www_hostname || "none"}
            size="compact"
          />
          <SummaryMetric
            label="redirect"
            value={domain.redirect_enabled ? "enabled" : "off"}
            size="compact"
          />
        </div>

        <SectionPanel
          title="Verification record"
          description="Publish this TXT record first, then verify."
        >
          <div className="grid gap-2 lg:grid-cols-2">
            <CopyValueRow label="TXT name" value={domain.verification_record_name} />
            <CopyValueRow label="TXT value" value={domain.verification_record_value} />
          </div>
        </SectionPanel>

        <SectionPanel
          title="Routing record"
          description="Point traffic at the hosted agent frontend."
        >
          <div className="grid gap-2 lg:grid-cols-2">
            <CopyValueRow label={routingLabel} value={domain.routing_record_name} />
            <CopyValueRow label="target" value={domain.routing_record_value} />
          </div>
        </SectionPanel>

        {domain.routing_fallback_record_type &&
          domain.routing_fallback_record_name &&
          domain.routing_fallback_record_value && (
            <SectionPanel
              title="Apex fallback"
              description={
                domain.routing_fallback_reason ||
                "Use this when your DNS provider cannot publish an apex CNAME/ALIAS record."
              }
            >
              <div className="grid gap-2 lg:grid-cols-2">
                <CopyValueRow
                  label={domain.routing_fallback_record_type}
                  value={domain.routing_fallback_record_name}
                />
                <CopyValueRow label="target" value={domain.routing_fallback_record_value} />
              </div>
            </SectionPanel>
          )}

        {domain.redirect_target_url && (
          <SectionPanel
            title="Redirect"
            description="Canonical traffic is redirected to this destination."
          >
            <CopyValueRow label="target URL" value={domain.redirect_target_url} />
          </SectionPanel>
        )}
      </div>
    </SurfacePanel>
  );
}

function domainRoutingLabel(domain: AgentCustomDomain) {
  return domain.dns_setup_kind === "apex-flattened-cname"
    ? "CNAME / ALIAS"
    : domain.routing_record_type;
}

function mergeAgentDomainRows(
  rows: AgentCustomDomain[],
  row: AgentCustomDomain,
): AgentCustomDomain[] {
  return [...rows.filter((existing) => existing.hostname !== row.hostname), row].sort(
    (a, b) => a.hostname.localeCompare(b.hostname),
  );
}

function domainStatusTone(status: string): StatusBadgeTone {
  if (status === "active") return "emerald";
  if (status === "verified") return "emerald";
  return "amber";
}

function formatDomainDate(value: string | null): string {
  if (!value) return "never";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
}

function normalizeHostname(value: string): string {
  return value.trim().toLowerCase().replace(/\.$/, "");
}
