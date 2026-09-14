import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import {
  createAgentIntegrationLink,
  deleteConsumerSetup,
  getConsumerSetup,
  integrationLinkUrlLabel,
  integrationLinkUrlSecrecyNote,
  listAgentIntegrationLinks,
  listInstalledAgents,
  revokeAgentIntegrationLink,
  upsertConsumerSetup,
  upsertOrgConsumerSetup,
  type AgentIntegrationLink,
  type AgentIntegrationLinkCreated,
  type ConsumerSetupField,
  type ConsumerSetupStatus,
  type ConsumerSetupValue,
  type InstalledAgent,
  type InstalledAgentSetupValue,
} from "../api";
import {
  CopyValueField,
  EmptyState,
  InlineAlert,
  LoadingState,
  SegmentedControl,
  SelectableSurfaceLink,
  SummaryMetric,
  SummaryStrip,
  SurfacePanel,
  TabLink,
  ToolbarButton,
  ToolbarLink,
  type StatusBadgeTone,
} from "./DashboardChrome";
import { StatusBadge } from "./StatusPillAdapters";
import {
  DASHBOARD_SECTION_CACHE_KEYS,
  useDashboardSectionResource,
} from "./DashboardSectionCache";
import { ConsumerSetupFieldInput } from "./ConsumerSetupFieldInput";
import { DetailSheet } from "./ListDetailLayout";
import {
  DashboardSurfacePosture,
  type SurfacePostureMetricTone,
  type SurfacePostureStatus,
} from "./SurfacePosture";
import { RoutePageShell } from "./RoutePageShell";
import {
  INSTALLED_AGENT_VIEWS,
  decodeRouteSegment,
  normalizeInstalledAgentViewId,
  type InstalledAgentViewId,
} from "../navigation";

type SaveScope = "user" | "org";
type ManageSetupScope = SaveScope;

const MANAGE_SETUP_SCOPES: Array<{
  id: ManageSetupScope;
  label: string;
  description: string;
}> = [
  {
    id: "user",
    label: "For me",
    description: "Save personal setup values that only apply to your account.",
  },
  {
    id: "org",
    label: "Organization",
    description: "Save shared defaults for everyone in the current organization.",
  },
];

function normalizeManageSetupScope(
  value: string | null | undefined,
): ManageSetupScope {
  return value === "org" ? "org" : "user";
}

function installedAgentRoute(
  agentName: string,
  view: InstalledAgentViewId = "setup",
) {
  const base = `/installed-setup/${encodeURIComponent(agentName)}`;
  return view === "setup" ? base : `${base}/${view}`;
}

function installedAgentManageRoute(
  agentName: string,
  scope: ManageSetupScope = "user",
) {
  return `/installed-setup/${encodeURIComponent(agentName)}/manage/${scope}`;
}

export function InstalledAgents() {
  const navigate = useNavigate();
  const {
    agentName: encodedAgentName,
    view: routeView,
    setupScope: routeSetupScope,
  } = useParams<{
    agentName?: string;
    view?: string;
    setupScope?: string;
  }>();
  const routeAgentName = decodeRouteSegment(encodedAgentName);
  const activeView = normalizeInstalledAgentViewId(routeView);
  const managingSetup = routeView === "manage";
  const activeManageScope = normalizeManageSetupScope(
    decodeRouteSegment(routeSetupScope),
  );
  const {
    data: agents,
    error: loadErr,
    refresh: refreshInstalledAgents,
  } = useDashboardSectionResource(
    DASHBOARD_SECTION_CACHE_KEYS.agents.installedSetup,
    listInstalledAgents,
  );
  const [actionErr, setActionErr] = useState<string | null>(null);
  const err = actionErr ?? loadErr;

  const load = useCallback(async () => {
    setActionErr(null);
    try {
      await refreshInstalledAgents();
    } catch {
      // The section cache stores and exposes the load error.
    }
  }, [refreshInstalledAgents]);

  const metrics = useMemo(() => {
    const rows = agents || [];
    const missingAgents = rows.filter((item) => item.missing_required.length > 0).length;
    return {
      installed: rows.length,
      setupValues: rows.reduce((sum, item) => sum + item.setup_values.length, 0),
      authConnections: rows.reduce((sum, item) => sum + item.auth_connections.length, 0),
      missing: rows.reduce((sum, item) => sum + item.missing_required.length, 0),
      missingAgents,
      ready: rows.length - missingAgents,
    };
  }, [agents]);
  const activeInstalled = useMemo(
    () =>
      routeAgentName && agents
        ? agents.find((item) => item.agent.name === routeAgentName) || null
        : null,
    [agents, routeAgentName],
  );
  const firstMissingInstalled = useMemo(
    () => agents?.find((item) => item.missing_required.length > 0) || null,
    [agents],
  );

  return (
    <RoutePageShell
      routeId="installed-agents"
      data-onboarding-target="installed-agents-page"
      actions={
        <ToolbarButton onClick={() => load()} disabled={agents === null}>
          Refresh
        </ToolbarButton>
      }
    >
      {err && <InlineAlert tone="red">{err}</InlineAlert>}

      <InstalledSetupPosture
        agents={agents}
        metrics={metrics}
        activeInstalled={activeInstalled}
        routeAgentName={routeAgentName}
        managingSetup={managingSetup}
        activeManageScope={activeManageScope}
        firstMissingInstalled={firstMissingInstalled}
      />

      {/* List stays mounted at all times; route-driven detail / manage views
          open IN PLACE as right-side sheets layered over it (mandate C). */}
      <div data-onboarding-target="installed-agents-list">
        {agents === null ? (
          <LoadingState label="Loading installed agents..." />
        ) : agents.length === 0 ? (
          <EmptyState
            title="No installed agents"
            description="Agents appear here after you save required setup values or connect credentials for an imported agent."
            action={
              <ToolbarLink href="/marketplace">
                Open marketplace
              </ToolbarLink>
            }
          />
        ) : (
          <div className="space-y-3">
            {agents.map((item) => (
              <InstalledAgentSummaryRow
                key={item.agent.id}
                item={item}
              />
            ))}
          </div>
        )}
      </div>

      {/* Detail sheet: route /installed-setup/:agentName[/:view] */}
      <DetailSheet
        open={Boolean(agents) && Boolean(routeAgentName) && !managingSetup}
        onClose={() => navigate("/installed-setup")}
        size="lg"
        title={
          <span className="font-mono [overflow-wrap:anywhere]">
            {activeInstalled?.agent.name ?? routeAgentName ?? "Installed agent"}
          </span>
        }
      >
        {routeAgentName && !managingSetup ? (
          activeInstalled ? (
            <InstalledAgentDetail
              item={activeInstalled}
              activeView={activeView}
              onManage={() =>
                navigate(installedAgentManageRoute(activeInstalled.agent.name))
              }
            />
          ) : (
            <InstalledAgentNotFound
              routeAgentName={routeAgentName}
              onBack={() => navigate("/installed-setup")}
            />
          )
        ) : null}
      </DetailSheet>

      {/* Manage (edit) sheet: route /installed-setup/:agentName/manage/:scope */}
      <DetailSheet
        open={Boolean(agents) && Boolean(routeAgentName) && managingSetup}
        onClose={() =>
          navigate(
            activeInstalled
              ? installedAgentRoute(activeInstalled.agent.name)
              : "/installed-setup",
          )
        }
        size="lg"
        title={
          <span className="font-mono [overflow-wrap:anywhere]">
            {activeInstalled?.agent.name ?? routeAgentName ?? "Manage setup"}
          </span>
        }
        description="Manage setup"
      >
        {routeAgentName && managingSetup ? (
          activeInstalled ? (
            <ManageSetupPage
              installed={activeInstalled}
              activeScope={activeManageScope}
              onChanged={() => load()}
            />
          ) : (
            <InstalledAgentNotFound
              routeAgentName={routeAgentName}
              onBack={() => navigate("/installed-setup")}
            />
          )
        ) : null}
      </DetailSheet>
    </RoutePageShell>
  );
}

function InstalledAgentNotFound({
  routeAgentName,
  onBack,
}: {
  routeAgentName: string;
  onBack: () => void;
}) {
  return (
    <div className="p-4">
      <div className="text-sm font-medium text-ink">Agent not found</div>
      <div className="mt-1 text-xs text-ink-muted">
        {routeAgentName} is not in the installed setup list.
      </div>
      <ToolbarButton className="mt-3" onClick={onBack}>
        Back to installed agents
      </ToolbarButton>
    </div>
  );
}

type InstalledSetupMetrics = {
  installed: number;
  setupValues: number;
  authConnections: number;
  missing: number;
  missingAgents: number;
  ready: number;
};

type InstalledSetupAction = {
  label: string;
  detail: string;
  href: string;
  action: string;
  variant: "primary" | "secondary";
};

function InstalledSetupPosture({
  agents,
  metrics,
  activeInstalled,
  routeAgentName,
  managingSetup,
  activeManageScope,
  firstMissingInstalled,
}: {
  agents: InstalledAgent[] | null;
  metrics: InstalledSetupMetrics;
  activeInstalled: InstalledAgent | null;
  routeAgentName: string | null;
  managingSetup: boolean;
  activeManageScope: ManageSetupScope;
  firstMissingInstalled: InstalledAgent | null;
}) {
  const nextAction = installedSetupNextAction({
    agents,
    activeInstalled,
    routeAgentName,
    managingSetup,
    activeManageScope,
    firstMissingInstalled,
    metrics,
  });
  const tone = installedSetupTone(agents, metrics, routeAgentName, activeInstalled);
  const label = installedSetupLabel(agents, metrics, routeAgentName, activeInstalled);
  const detail = installedSetupDetail({
    agents,
    metrics,
    routeAgentName,
    activeInstalled,
    managingSetup,
    activeManageScope,
  });
  const metricsLoaded = agents !== null;
  const readyMetricTone: SurfacePostureMetricTone = !metricsLoaded
    ? "neutral"
    : metrics.missingAgents > 0
      ? "authority"
      : "live";
  const authMetricTone: SurfacePostureMetricTone =
    metricsLoaded && metrics.authConnections > 0 ? "live" : "neutral";
  const missingMetricTone: SurfacePostureMetricTone =
    metricsLoaded && metrics.missing > 0 ? "authority" : "neutral";

  return (
    <DashboardSurfacePosture
      data-onboarding-target="installed-setup-posture"
      aria-label="Installed setup posture summary"
      eyebrow="Installed setup"
      title={nextAction.label}
      status={{
        label,
        tone: postureStatusTone(tone),
        dot: tone === "emerald" || tone === "amber",
      }}
      metrics={[
        {
          label: "installed",
          value: metricsLoaded ? metrics.installed.toLocaleString() : "—",
        },
        {
          label: "ready",
          value: metricsLoaded ? metrics.ready.toLocaleString() : "—",
          tone: readyMetricTone,
        },
        {
          label: "auth",
          value: metricsLoaded ? metrics.authConnections.toLocaleString() : "—",
          tone: authMetricTone,
        },
        {
          label: "missing",
          value: metricsLoaded ? metrics.missing.toLocaleString() : "—",
          tone: missingMetricTone,
        },
      ]}
      actions={
        metricsLoaded ? (
          <div className="flex flex-wrap items-center gap-2">
            <span className="hidden max-w-md truncate text-xs text-ink-muted xl:inline">
              {detail}
            </span>
            <ToolbarLink href={nextAction.href} variant={nextAction.variant} size="sm">
              {nextAction.action}
            </ToolbarLink>
          </div>
        ) : null
      }
    />
  );
}

function postureStatusTone(
  tone: StatusBadgeTone,
): SurfacePostureStatus["tone"] {
  if (tone === "emerald") return "live";
  if (tone === "amber") return "authority";
  if (tone === "red") return "danger";
  return "neutral";
}

function InstalledAgentDecisionPanel({
  item,
  activeView,
  onManage,
}: {
  item: InstalledAgent;
  activeView: InstalledAgentViewId;
  onManage: () => void;
}) {
  const nextAction = installedAgentNextAction(item, activeView);

  return (
    <div className="mt-4 grid gap-4 border-t border-runtime-line-soft/70 pt-4">
      <SummaryStrip className="lg:grid-cols-5" aria-label={`${item.agent.name} setup decision summary`}>
        <SummaryMetric
          label="setup"
          value={item.setup_values.length.toLocaleString()}
          detail="saved values"
          size="compact"
        />
        <SummaryMetric
          label="missing"
          value={item.missing_required.length.toLocaleString()}
          detail="required fields"
          tone={item.missing_required.length > 0 ? "amber" : "emerald"}
          size="compact"
        />
        <SummaryMetric
          label="auth"
          value={item.auth_connections.length.toLocaleString()}
          detail="connections"
          tone={item.auth_connections.length > 0 ? "emerald" : "neutral"}
          size="compact"
        />
        <SummaryMetric
          label="status"
          value={item.agent.status}
          detail={item.agent.public ? "public" : "private"}
          tone={agentStatusTone(item.agent.status)}
          size="compact"
        />
        <SummaryMetric
          label="updated"
          value={formatDate(item.updated_at)}
          detail="setup record"
          size="compact"
        />
      </SummaryStrip>

      <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
        <div className="min-w-0">
          <div className="text-[10px] font-medium uppercase tracking-wide text-ink-faint">
            Decision cue
          </div>
          <div className="mt-1 text-sm font-medium text-ink">
            {nextAction.label}
          </div>
          <p className="mt-1 max-w-2xl text-xs leading-relaxed text-ink-muted">
            {nextAction.detail}
          </p>
        </div>
        {nextAction.kind === "manage" ? (
          <ToolbarButton onClick={onManage} variant="primary" size="md">
            {nextAction.action}
          </ToolbarButton>
        ) : (
          <ToolbarLink href={nextAction.href} variant={nextAction.variant} size="md">
            {nextAction.action}
          </ToolbarLink>
        )}
      </div>
    </div>
  );
}

function InstalledAgentSummaryRow({ item }: {
  item: InstalledAgent;
}) {
  return (
    <SelectableSurfaceLink
      href={installedAgentRoute(item.agent.name)}
      className="p-4 hover:border-runtime-line-strong hover:bg-runtime-panel/70"
    >
      <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0 flex-1">
          <InstalledAgentTitle item={item} />
          <InstalledAgentDescription item={item} />
        </div>
        <div className="grid w-full grid-cols-3 gap-2 text-xs sm:w-auto sm:min-w-[300px]">
          <SummaryMetric label="setup" value={item.setup_values.length} size="compact" />
          <SummaryMetric label="auth" value={item.auth_connections.length} size="compact" />
          <SummaryMetric
            label="missing"
            value={item.missing_required.length}
            tone={item.missing_required.length ? "amber" : "neutral"}
            size="compact"
          />
        </div>
      </div>
    </SelectableSurfaceLink>
  );
}

function InstalledAgentDetail({
  item,
  activeView,
  onManage,
}: {
  item: InstalledAgent;
  activeView: InstalledAgentViewId;
  onManage: () => void;
}) {
  return (
    <div className="min-w-0">
      <div className="border-b border-runtime-line-soft/60 px-4 py-3">
        <div className="flex flex-col gap-2 lg:flex-row lg:items-start lg:justify-between">
          <div className="min-w-0">
            <InstalledAgentTitle item={item} />
            <InstalledAgentDescription item={item} />
          </div>
          {activeView === "setup" && (
            <ToolbarButton onClick={onManage}>Manage setup</ToolbarButton>
          )}
        </div>
        <InstalledAgentDecisionPanel
          item={item}
          activeView={activeView}
          onManage={onManage}
        />
        <SegmentedControl
          role="tablist"
          aria-label={`${item.agent.name} installed setup views`}
          className="mt-3 flex gap-1 overflow-x-auto bg-runtime-panel/60"
        >
          {INSTALLED_AGENT_VIEWS.map((view) => (
            <TabLink
              key={view.id}
              href={installedAgentRoute(item.agent.name, view.id)}
              selected={activeView === view.id}
              className="whitespace-nowrap"
            >
              {view.label}
            </TabLink>
          ))}
        </SegmentedControl>
      </div>

      {activeView === "setup" && <InstalledAgentSetupValues item={item} />}
      {activeView === "auth" && <InstalledAgentAuthConnections item={item} />}
      {activeView === "links" && <InstalledAgentIntegrationLinks agentName={item.agent.name} />}
    </div>
  );
}

function InstalledAgentTitle({ item }: { item: InstalledAgent }) {
  return (
    <div className="flex flex-wrap items-center gap-2">
      <h2 className="truncate text-sm font-semibold text-ink">
        {item.agent.name}
      </h2>
      <StatusBadge tone={agentStatusTone(item.agent.status)} dot>
        {item.agent.status}
      </StatusBadge>
      {!item.agent.public && <StatusBadge>private</StatusBadge>}
      {item.missing_required.length > 0 && (
        <StatusBadge tone="amber">{item.missing_required.length} missing</StatusBadge>
      )}
    </div>
  );
}

function InstalledAgentDescription({ item }: { item: InstalledAgent }) {
  return (
    <>
      {item.agent.description && (
        <p className="mt-1 line-clamp-2 text-sm leading-relaxed text-ink-muted">
          {item.agent.description}
        </p>
      )}
      <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-xs text-ink-faint">
        <span>{item.setup_values.length} setup values</span>
        <span>{item.auth_connections.length} auth connections</span>
        <span>Updated {formatDate(item.updated_at)}</span>
      </div>
    </>
  );
}

function InstalledAgentSetupValues({ item }: { item: InstalledAgent }) {
  const hasSetup = item.setup_values.length > 0;
  return (
    <div className="p-4">
      <h3 className="text-xs font-medium uppercase text-ink-faint">
        Setup values
      </h3>
      {!hasSetup ? (
        <EmptyState
          size="compact"
          title="No consumer setup values saved"
          className="mt-3 bg-runtime-panel/20 py-6"
        />
      ) : (
        <div className="mt-3 divide-y divide-runtime-line-soft">
          {item.setup_values.map((value) => (
            <SetupValueRow key={`${value.scope}:${value.organization?.slug || ""}:${value.name}`} value={value} />
          ))}
        </div>
      )}
    </div>
  );
}

function InstalledAgentAuthConnections({ item }: { item: InstalledAgent }) {
  const hasAuth = item.auth_connections.length > 0;
  return (
    <div className="p-4">
      <h3 className="text-xs font-medium uppercase text-ink-faint">
        Imported auth
      </h3>
      {!hasAuth ? (
        <EmptyState
          size="compact"
          title="No imported auth connections"
          className="mt-3 bg-runtime-panel/20 py-6"
        />
      ) : (
        <div className="mt-3 divide-y divide-runtime-line-soft">
          {item.auth_connections.map((connection) => (
            <div key={connection.id} className="py-3 first:pt-0 last:pb-0">
              <div className="flex flex-wrap items-center gap-2">
                <span className="min-w-0 truncate font-mono text-xs text-ink">
                  {connection.scheme_name}
                </span>
                <StatusBadge tone={connectionStatusTone(connection.status)}>
                  {connection.status}
                </StatusBadge>
              </div>
              <div className="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-xs text-ink-faint">
                <span>{connection.scheme_type}</span>
                <span>{connection.credential_scope}</span>
                <span>
                  {connection.expires_at
                    ? `Expires ${formatDate(connection.expires_at)}`
                    : "No expiry"}
                </span>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function InstalledAgentIntegrationLinks({ agentName }: { agentName: string }) {
  const [links, setLinks] = useState<AgentIntegrationLink[] | null>(null);
  const [newLink, setNewLink] = useState<AgentIntegrationLinkCreated | null>(null);
  const [urlToken, setUrlToken] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setLinks(await listAgentIntegrationLinks(agentName));
      setErr(null);
    } catch (ex) {
      setErr(messageFromError(ex));
    }
  }, [agentName]);

  useEffect(() => {
    load();
  }, [load]);

  async function createLink() {
    if (busy) return;
    setBusy("create");
    setErr(null);
    setNewLink(null);
    try {
      const link = await createAgentIntegrationLink(agentName, { urlToken });
      setNewLink(link);
      setLinks((current) => [link, ...(current || [])]);
    } catch (ex) {
      setErr(messageFromError(ex));
    } finally {
      setBusy(null);
    }
  }

  async function deleteLink(link: AgentIntegrationLink) {
    if (busy) return;
    setBusy(`delete:${link.id}`);
    setErr(null);
    try {
      await revokeAgentIntegrationLink(agentName, link.id);
      setLinks((current) =>
        (current || []).map((item) =>
          item.id === link.id ? { ...item, enabled: false } : item,
        ),
      );
    } catch (ex) {
      setErr(messageFromError(ex));
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="border-t border-runtime-line-soft/60 p-4">
      <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
        <div>
          <h3 className="text-xs font-medium uppercase text-ink-faint">
            Integration links
          </h3>
          <p className="mt-1 text-sm text-ink-muted">
            A bearer token for MCP and direct API integrations, sent as{" "}
            <span className="font-mono">Authorization: Bearer &lt;token&gt;</span>.
            Calls use your saved setup.{" "}
            {integrationLinkUrlSecrecyNote(urlToken)}
          </p>
        </div>
        <ToolbarButton onClick={createLink} disabled={Boolean(busy)}>
          {busy === "create" ? "Generating..." : "Generate link"}
        </ToolbarButton>
      </div>

      <label className="mt-3 flex items-start gap-2 text-xs text-ink-muted">
        <input
          type="checkbox"
          checked={urlToken}
          onChange={(event) => setUrlToken(event.target.checked)}
          disabled={Boolean(busy)}
          className="mt-0.5"
        />
        <span>
          Put the token in the URL instead (lower security) — only for MCP or
          OpenAPI clients that cannot send a header. URL tokens leak through
          access logs, browser history and Referer headers, so they are given a
          shorter lifetime.
        </span>
      </label>

      {err && <div className="mt-3"><InlineAlert tone="red">{err}</InlineAlert></div>}

      {newLink && (
        <div className="mt-3 grid gap-3">
          {newLink.security_notice && (
            <InlineAlert tone="amber" className="text-xs">
              {newLink.security_notice}
            </InlineAlert>
          )}
          <div className="grid gap-3 lg:grid-cols-2">
            <CopyValueField label="Bearer token" value={newLink.token} />
            <CopyValueField label="curl" value={newLink.curl_example} multiline />
            <CopyValueField
              label={integrationLinkUrlLabel("OpenAPI URL", newLink)}
              value={newLink.openapi_url}
            />
            <CopyValueField
              label={integrationLinkUrlLabel("MCP URL", newLink)}
              value={newLink.mcp_url}
            />
            {newLink.sample_invoke_url && (
              <CopyValueField
                label={integrationLinkUrlLabel("Sample invoke URL", newLink)}
                value={newLink.sample_invoke_url}
              />
            )}
          </div>
          {newLink.expires_at && (
            <div className="text-xs text-ink-muted">
              Expires {formatDate(newLink.expires_at)}. Generate a new link before
              then; delete this one to revoke it immediately.
            </div>
          )}
        </div>
      )}

      <div className="mt-3">
        {links === null ? (
          <LoadingState label="Loading integration links..." />
        ) : links.length === 0 ? (
          <EmptyState
            size="compact"
            title="No integration links generated"
            className="bg-runtime-panel/20 py-6"
          />
        ) : (
          <div className="grid gap-2">
            {links.map((link) => (
              <SurfacePanel
                as="div"
                key={link.id}
                className="flex flex-col gap-2 bg-runtime-panel/40 px-3 py-2 text-xs sm:flex-row sm:items-center sm:justify-between"
              >
                <div className="min-w-0">
                  <div className="text-ink-soft">Integration link</div>
                  <div className="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-ink-muted">
                    <span className="font-mono">...{link.token_last4}</span>
                    <span>{link.enabled ? "enabled" : "revoked"}</span>
                    <span>
                      {link.expires_at
                        ? `expires ${formatDate(link.expires_at)}`
                        : "no expiry"}
                    </span>
                    <span>{link.last_used_at ? `used ${formatDate(link.last_used_at)}` : "never used"}</span>
                  </div>
                </div>
                <ToolbarButton
                  variant="danger"
                  onClick={() => deleteLink(link)}
                  disabled={!link.enabled || Boolean(busy)}
                >
                  {busy === `delete:${link.id}` ? "Deleting..." : "Delete"}
                </ToolbarButton>
              </SurfacePanel>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function SetupValueRow({ value }: { value: InstalledAgentSetupValue }) {
  return (
    <div className="grid gap-2 py-3 first:pt-0 last:pb-0 sm:grid-cols-[minmax(0,1fr)_minmax(10rem,auto)] sm:items-center">
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          <span className="truncate text-sm font-medium text-ink-soft">
            {value.label || value.name}
          </span>
          {value.required && <StatusBadge tone="amber">required</StatusBadge>}
          <StatusBadge>{value.kind}</StatusBadge>
        </div>
        <div className="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-xs text-ink-faint">
          <span>{value.scope === "org" ? value.organization?.name || "Organization" : "User"}</span>
          <span className="font-mono">{value.name}</span>
        </div>
      </div>
      <SummaryMetric
        label="value"
        value={value.value_redacted || "configured"}
        size="compact"
      />
    </div>
  );
}

function ManageSetupPage({
  installed,
  activeScope,
  onChanged,
}: {
  installed: InstalledAgent;
  activeScope: ManageSetupScope;
  onChanged: (agentName: string) => void | Promise<void>;
}) {
  const [status, setStatus] = useState<ConsumerSetupStatus | null>(null);
  const [values, setValues] = useState<Record<string, unknown>>({});
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState<SaveScope | "clear" | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    setLoading(true);
    setErr(null);
    getConsumerSetup(installed.agent.name)
      .then((next) => {
        if (!active) return;
        setStatus(next);
        setValues(initialSetupValues(next));
      })
      .catch((ex) => {
        if (active) setErr(messageFromError(ex));
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [installed.agent.name]);

  async function save(scope: SaveScope) {
    if (!status) return;
    const payload = payloadSetupValues(status.declaration.fields, values);
    if (Object.keys(payload).length === 0) {
      setErr("Enter at least one value to save.");
      return;
    }
    setSaving(scope);
    setErr(null);
    try {
      const next =
        scope === "org"
          ? await upsertOrgConsumerSetup(installed.agent.name, payload, status.organization?.slug)
          : await upsertConsumerSetup(installed.agent.name, payload);
      setStatus(next);
      setValues(initialSetupValues(next));
      await onChanged(installed.agent.name);
    } catch (ex) {
      setErr(messageFromError(ex));
    } finally {
      setSaving(null);
    }
  }

  async function clearValue(value: ConsumerSetupValue) {
    if (!status || (value.source !== "user" && value.source !== "org")) return;
    const source = value.source;
    const matchingInstalledValue = installed.setup_values.find(
      (item) => item.name === value.name && item.scope === source,
    );
    const organizationSlug =
      source === "org"
        ? matchingInstalledValue?.organization?.slug || status.organization?.slug
        : undefined;
    setSaving("clear");
    setErr(null);
    try {
      const next = await deleteConsumerSetup(
        installed.agent.name,
        value.name,
        source,
        organizationSlug,
      );
      setStatus(next);
      setValues(initialSetupValues(next));
      await onChanged(installed.agent.name);
    } catch (ex) {
      setErr(messageFromError(ex));
    } finally {
      setSaving(null);
    }
  }

  const fields = status?.declaration.fields || [];
  const valueByName = new Map((status?.values || []).map((value) => [value.name, value]));
  const missing = new Set(status?.missing_required || []);
  const activeScopeMeta =
    MANAGE_SETUP_SCOPES.find((scope) => scope.id === activeScope) ||
    MANAGE_SETUP_SCOPES[0];
  const canSaveActiveScope =
    activeScope === "user" || Boolean(status?.can_manage_org);
  const organizationName = status?.organization?.name || "organization";
  const saveLabel =
    activeScope === "org" ? `Save for ${organizationName}` : "Save for me";

  return (
    <div className="min-w-0">
      <div className="border-b border-runtime-line-soft/60 px-4 py-3">
        <div className="flex flex-col gap-2 lg:flex-row lg:items-start lg:justify-between">
          <p className="min-w-0 max-w-2xl text-xs leading-relaxed text-ink-muted">
            {activeScopeMeta.description} Saved values are shown redacted.
            Enter a new value to replace an existing one.
          </p>
          <div className="flex shrink-0 flex-wrap gap-2">
            <ToolbarButton
              variant="primary"
              onClick={() => save(activeScope)}
              disabled={
                loading ||
                saving !== null ||
                fields.length === 0 ||
                !canSaveActiveScope
              }
            >
              {saving === activeScope ? "Saving..." : saveLabel}
            </ToolbarButton>
          </div>
        </div>
        <SegmentedControl
          role="tablist"
          aria-label={`${installed.agent.name} setup management scope`}
          className="mt-3 flex gap-1 overflow-x-auto bg-runtime-panel/60"
        >
          {MANAGE_SETUP_SCOPES.map((scope) => (
            <TabLink
              key={scope.id}
              href={installedAgentManageRoute(installed.agent.name, scope.id)}
              selected={activeScope === scope.id}
              className="whitespace-nowrap"
            >
              {scope.label}
            </TabLink>
          ))}
        </SegmentedControl>
      </div>

      <div className="space-y-4 p-4">
        {loading && <LoadingState label="Loading setup..." />}
        {err && <InlineAlert tone="red">{err}</InlineAlert>}
        {activeScope === "org" && status && !status.can_manage_org && (
          <InlineAlert tone="amber">
            You do not have permission to save organization setup values for this agent.
          </InlineAlert>
        )}

        {!loading && fields.length === 0 && (
          <InlineAlert tone="neutral">
            This agent has no A2APack consumer setup fields. Imported auth credentials are managed from My agents.
          </InlineAlert>
        )}

        {fields.length > 0 && (
          <div className="space-y-4">
            <div className="flex flex-wrap items-center gap-2">
              <StatusBadge tone={status?.complete ? "emerald" : "amber"}>
                {status?.complete ? "complete" : `${missing.size} missing`}
              </StatusBadge>
              {status?.organization && (
                <StatusBadge>{status.organization.name}</StatusBadge>
              )}
            </div>

            <SurfacePanel
              as="div"
              className="divide-y divide-runtime-line-soft overflow-hidden bg-runtime-bg/40"
            >
              {fields.map((field) => {
                const current = valueByName.get(field.name);
                const configured = Boolean(current?.configured);
                const canClear =
                  configured &&
                  current?.source === activeScope &&
                  canSaveActiveScope;
                return (
                  <div key={field.name} className="p-4">
                    <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
                      <div className="min-w-0">
                        <div className="flex flex-wrap items-center gap-2">
                          <span className="text-sm font-medium text-ink">
                            {field.label || field.name}
                          </span>
                          {field.required && <StatusBadge tone="amber">required</StatusBadge>}
                          {missing.has(field.name) && <StatusBadge tone="amber">missing</StatusBadge>}
                        </div>
                        {field.description && (
                          <p className="mt-1 text-xs leading-relaxed text-ink-muted">
                            {field.description}
                          </p>
                        )}
                        {configured && (
                          <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-ink-muted">
                            <span>{current?.source || "configured"}</span>
                            <span className="rounded-md border border-runtime-line-soft/60 bg-runtime-panel/50 px-2 py-1 font-mono text-ink-soft">
                              {current?.value_redacted || "configured"}
                            </span>
                          </div>
                        )}
                      </div>
                      {canClear && current && (
                        <ToolbarButton
                          variant="danger"
                          size="xs"
                          onClick={() => clearValue(current)}
                          disabled={saving !== null}
                        >
                          {saving === "clear" ? "Clearing..." : "Clear"}
                        </ToolbarButton>
                      )}
                    </div>
                    <ConsumerSetupFieldInput
                      field={field}
                      value={values[field.name]}
                      attention={missing.has(field.name)}
                      disabled={loading || saving !== null || !canSaveActiveScope}
                      onChange={(value) =>
                        setValues((currentValues) => ({
                          ...currentValues,
                          [field.name]: value,
                        }))
                      }
                    />
                  </div>
                );
              })}
            </SurfacePanel>
          </div>
        )}
      </div>
    </div>
  );
}

function initialSetupValues(status: ConsumerSetupStatus): Record<string, unknown> {
  const configured = new Set(
    status.values.filter((value) => value.configured).map((value) => value.name),
  );
  const out: Record<string, unknown> = {};
  for (const field of status.declaration.fields) {
    if (!configured.has(field.name)) out[field.name] = "";
  }
  return out;
}

function payloadSetupValues(
  fields: ConsumerSetupField[],
  values: Record<string, unknown>,
): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const field of fields) {
    const value = values[field.name];
    if (value === undefined || value === null || value === "") continue;
    out[field.name] = value;
  }
  return out;
}

function installedSetupNextAction({
  agents,
  activeInstalled,
  routeAgentName,
  managingSetup,
  activeManageScope,
  firstMissingInstalled,
  metrics,
}: {
  agents: InstalledAgent[] | null;
  activeInstalled: InstalledAgent | null;
  routeAgentName: string | null;
  managingSetup: boolean;
  activeManageScope: ManageSetupScope;
  firstMissingInstalled: InstalledAgent | null;
  metrics: InstalledSetupMetrics;
}): InstalledSetupAction {
  if (!agents) {
    return {
      label: "Load installed setup",
      detail: "Fetching installed agents, saved setup values, auth connections, and required-field gaps.",
      href: "/installed-setup",
      action: "Installed setup",
      variant: "secondary",
    };
  }
  if (agents.length === 0) {
    return {
      label: "Install an agent from Marketplace",
      detail: "Saved setup appears here after an agent is installed or configured from Marketplace.",
      href: "/marketplace",
      action: "Open marketplace",
      variant: "primary",
    };
  }
  if (routeAgentName && !activeInstalled) {
    return {
      label: "Return to installed agents",
      detail: `${routeAgentName} is not in the installed setup list.`,
      href: "/installed-setup",
      action: "All installed",
      variant: "primary",
    };
  }
  if (activeInstalled) {
    if (managingSetup) {
      return {
        label: `Save ${activeManageScope} setup values`,
        detail: `You are editing ${activeManageScope === "org" ? "organization" : "personal"} setup values for ${activeInstalled.agent.name}.`,
        href: installedAgentRoute(activeInstalled.agent.name),
        action: "View setup",
        variant: "secondary",
      };
    }
    if (activeInstalled.missing_required.length > 0) {
      return {
        label: "Fill required setup",
        detail: `${activeInstalled.agent.name} is missing ${activeInstalled.missing_required.length} required setup field${activeInstalled.missing_required.length === 1 ? "" : "s"}.`,
        href: installedAgentManageRoute(activeInstalled.agent.name),
        action: "Manage setup",
        variant: "primary",
      };
    }
    return {
      label: "Review integration readiness",
      detail: `${activeInstalled.agent.name} has setup ready; review auth connections or generate integration links for external use.`,
      href: installedAgentRoute(activeInstalled.agent.name, activeInstalled.auth_connections.length > 0 ? "links" : "auth"),
      action: activeInstalled.auth_connections.length > 0 ? "Open links" : "Open auth",
      variant: "secondary",
    };
  }
  if (firstMissingInstalled) {
    return {
      label: "Resolve required setup gaps",
      detail: `${firstMissingInstalled.agent.name} is one of ${metrics.missingAgents} agents with missing required setup.`,
      href: installedAgentManageRoute(firstMissingInstalled.agent.name),
      action: "Fix setup",
      variant: "primary",
    };
  }
  const first = agents[0];
  return {
    label: "Review installed setup",
    detail: `${metrics.ready} installed agent${metrics.ready === 1 ? "" : "s"} have no required setup gaps.`,
    href: installedAgentRoute(first.agent.name),
    action: "Open first agent",
    variant: "secondary",
  };
}

function installedSetupTone(
  agents: InstalledAgent[] | null,
  metrics: InstalledSetupMetrics,
  routeAgentName: string | null,
  activeInstalled: InstalledAgent | null,
) {
  if (!agents) return "neutral" as const;
  if (routeAgentName && !activeInstalled) return "red" as const;
  if (metrics.missing > 0) return "amber" as const;
  if (metrics.installed > 0) return "emerald" as const;
  return "neutral" as const;
}

function installedSetupLabel(
  agents: InstalledAgent[] | null,
  metrics: InstalledSetupMetrics,
  routeAgentName: string | null,
  activeInstalled: InstalledAgent | null,
) {
  if (!agents) return "loading";
  if (routeAgentName && !activeInstalled) return "not found";
  if (metrics.installed === 0) return "empty";
  if (metrics.missing > 0) return "setup needed";
  return "ready";
}

function installedSetupDetail({
  agents,
  metrics,
  routeAgentName,
  activeInstalled,
  managingSetup,
  activeManageScope,
}: {
  agents: InstalledAgent[] | null;
  metrics: InstalledSetupMetrics;
  routeAgentName: string | null;
  activeInstalled: InstalledAgent | null;
  managingSetup: boolean;
  activeManageScope: ManageSetupScope;
}) {
  if (!agents) return "loading saved setup";
  if (routeAgentName && !activeInstalled) return `${routeAgentName} is not installed`;
  if (activeInstalled) {
    return managingSetup
      ? `editing ${activeManageScope} setup for ${activeInstalled.agent.name}`
      : `focused on ${activeInstalled.agent.name}`;
  }
  if (metrics.missing > 0) {
    return `${metrics.missing} missing required setup value${metrics.missing === 1 ? "" : "s"}`;
  }
  return `${metrics.installed} installed agent${metrics.installed === 1 ? "" : "s"}, ${metrics.authConnections} auth connection${metrics.authConnections === 1 ? "" : "s"}`;
}

function installedAgentNextAction(
  item: InstalledAgent,
  activeView: InstalledAgentViewId,
):
  | {
      kind: "manage";
      label: string;
      detail: string;
      action: string;
    }
  | {
      kind: "link";
      label: string;
      detail: string;
      href: string;
      action: string;
      variant: "primary" | "secondary";
    } {
  if (item.missing_required.length > 0) {
    return {
      kind: "manage",
      label: "Complete required setup",
      detail: `${item.agent.name} cannot run with all expected setup until ${item.missing_required.length} required field${item.missing_required.length === 1 ? "" : "s"} are saved.`,
      action: "Manage setup",
    };
  }
  if (activeView === "setup") {
    return {
      kind: "link",
      label: "Check imported auth",
      detail: "Setup values are present; review imported auth connections before exposing integration links.",
      href: installedAgentRoute(item.agent.name, "auth"),
      action: "Open auth",
      variant: "secondary",
    };
  }
  if (activeView === "auth" && item.auth_connections.length === 0) {
    return {
      kind: "link",
      label: "No imported auth connected",
      detail: "This agent has setup values but no imported auth connections in this account.",
      href: `/my-agents/${encodeURIComponent(item.agent.name)}/auth`,
      action: "Open My agents",
      variant: "secondary",
    };
  }
  return {
    kind: "link",
    label: "Use integration links",
    detail: "Generate or review long secret URLs that call this agent with saved setup.",
    href: installedAgentRoute(item.agent.name, "links"),
    action: "Open links",
    variant: "secondary",
  };
}

function agentStatusTone(status: string): StatusBadgeTone {
  if (status === "running") return "emerald";
  if (status === "failed" || status === "error") return "red";
  if (status === "needs_auth" || status === "pending") return "amber";
  return "neutral";
}

function connectionStatusTone(status: string): StatusBadgeTone {
  if (status === "connected") return "emerald";
  if (status === "expired" || status === "failed") return "red";
  if (status === "needs_setup") return "amber";
  return "neutral";
}

function formatDate(value: string | null) {
  if (!value) return "never";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  }).format(date);
}

function messageFromError(ex: unknown) {
  return ex instanceof Error ? ex.message : String(ex);
}
