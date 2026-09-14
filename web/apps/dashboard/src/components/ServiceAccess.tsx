import { useCallback, useEffect, useState } from "react";
import { useLocation, useNavigate, useParams } from "react-router-dom";
import {
  getServiceAccess,
  type GiteaRepositoryAccess,
  type OrganizationServiceAccess,
  type ServiceAccess as ServiceAccessData,
} from "../api";
import {
  EmptyState,
  InlineAlert,
  ReadOnlyValueField,
  SectionPanel,
  SegmentedButton,
  SegmentedControl,
  SelectableSurfaceLink,
  SummaryMetric,
  SurfacePanel,
  ToolbarButton,
  ToolbarLink,
} from "./DashboardChrome";
import { StateBadge, StatusBadge } from "./StatusPillAdapters";
import {
  DASHBOARD_SECTION_CACHE_KEYS,
  useDashboardSectionResource,
} from "./DashboardSectionCache";
import {
  ACCESS_VIEWS,
  type AccessViewId,
  accessViewForPath,
  decodeRouteSegment,
} from "../navigation";
import { RoutePageShell } from "./RoutePageShell";
import { DashboardSurfacePosture } from "./SurfacePosture";
import { DetailSheet } from "./ListDetailLayout";

function accessRepositoryRoute(agentName: string) {
  return `/access/repositories/${encodeURIComponent(agentName)}`;
}

function accessLangfuseOrgRoute(orgSlug: string) {
  return `/access/langfuse/${encodeURIComponent(orgSlug)}`;
}

function accessLiteLlmOrgRoute(orgSlug: string) {
  return `/access/litellm/${encodeURIComponent(orgSlug)}`;
}

function accessGiteaOrgRoute(orgSlug: string) {
  return `/access/gitea/${encodeURIComponent(orgSlug)}`;
}

const accessPostureToneMap = {
  emerald: "live",
  amber: "authority",
  red: "danger",
  neutral: "neutral",
} as const;

export function ServiceAccess() {
  const location = useLocation();
  const navigate = useNavigate();
  const {
    agentName: encodedAgentName,
    orgSlug: encodedOrgSlug,
  } = useParams<{ agentName?: string; orgSlug?: string }>();
  const selectedRepositoryAgentName = decodeRouteSegment(encodedAgentName);
  const selectedAccessOrgSlug = decodeRouteSegment(encodedOrgSlug);
  // Deep-link hydration (mandate B/deep-links): the active view-mode follows the
  // URL segment, but switching modes is a local navigation — the page never
  // unmounts and the section-cached access data is never refetched.
  const routeView = accessViewForPath(location.pathname).id;
  const [activeView, setActiveView] = useState<AccessViewId>(routeView);
  useEffect(() => {
    setActiveView(routeView);
  }, [routeView]);
  const selectView = useCallback(
    (id: AccessViewId) => {
      setActiveView(id);
      const target = ACCESS_VIEWS.find((v) => v.id === id);
      if (target) navigate(target.path);
    },
    [navigate],
  );
  const {
    data,
    error: err,
    refresh: refreshAccess,
  } = useDashboardSectionResource(
    DASHBOARD_SECTION_CACHE_KEYS.settings.access,
    getServiceAccess,
  );

  const refresh = useCallback(async () => {
    try {
      await refreshAccess();
    } catch {
      // The section cache stores and exposes the load error.
    }
  }, [refreshAccess]);

  return (
    <RoutePageShell
      routeId="access"
      data-onboarding-target="access-page"
      actions={
        <ToolbarButton onClick={refresh} size="md">
          Refresh
        </ToolbarButton>
      }
    >
      {err && <InlineAlert tone="red">{err}</InlineAlert>}

      {data === null ? (
        <AccessSkeleton />
      ) : (
        <div data-onboarding-target="access-services" className="space-y-4">
          <AccessPostureStrip data={data} />

          {/* Mandate D: the five former route panels collapse into one
              keep-alive view with an internal view-mode selector. The page
              never unmounts; the active mode also reflects in the URL so nav
              highlighting (mandate A) and deep-links stay correct. */}
          <SegmentedControl aria-label="Access views" className="w-full">
            {ACCESS_VIEWS.map((tab) => (
              <SegmentedButton
                key={tab.id}
                selected={activeView === tab.id}
                onClick={() => selectView(tab.id)}
              >
                {tab.label}
              </SegmentedButton>
            ))}
          </SegmentedControl>

          <div className="min-w-0">
            {activeView === "overview" && <AccessOverview data={data} />}
            {activeView === "langfuse" && (
              <LangfuseSection
                data={data}
                selectedOrgSlug={selectedAccessOrgSlug}
                onCloseDetail={() => navigate("/access/langfuse")}
              />
            )}
            {activeView === "litellm" && (
              <LiteLlmSection
                data={data}
                selectedOrgSlug={selectedAccessOrgSlug}
                onCloseDetail={() => navigate("/access/litellm")}
              />
            )}
            {activeView === "gitea" && (
              <GiteaSection
                data={data}
                selectedOrgSlug={selectedAccessOrgSlug}
                onCloseDetail={() => navigate("/access/gitea")}
              />
            )}
            {activeView === "repositories" && (
              <RepositoriesSection
                data={data}
                selectedAgentName={selectedRepositoryAgentName}
                onCloseDetail={() => navigate("/access/repositories")}
              />
            )}
          </div>
        </div>
      )}
    </RoutePageShell>
  );
}

function AccessPostureStrip({ data }: { data: ServiceAccessData }) {
  const posture = accessPosture(data);
  return (
    <DashboardSurfacePosture
      data-onboarding-target="access-posture"
      eyebrow="Service access"
      title="Service access posture"
      status={{
        label: posture.label,
        tone: accessPostureToneMap[posture.tone],
        dot: posture.tone === "emerald",
      }}
      metrics={[
        {
          label: "orgs",
          value: data.organizations.length.toLocaleString(),
        },
        {
          label: "checks",
          value: `${posture.readyCount}/${posture.serviceCount}`,
          tone: accessPostureToneMap[posture.tone],
        },
        {
          label: "repos",
          value: data.gitea.repositories.length.toLocaleString(),
        },
        {
          label: "errors",
          value: posture.errorCount.toLocaleString(),
          tone: posture.errorCount > 0 ? "authority" : "live",
        },
      ]}
      actions={
        <>
          <ToolbarLink href="/access/repositories" size="sm">
            Repositories
          </ToolbarLink>
          <ExternalLink href={data.gitea.base_url}>Open Gitea</ExternalLink>
        </>
      }
    />
  );
}

function AccessSkeleton() {
  return (
    <div className="space-y-4" aria-busy="true" aria-label="Loading access">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2 rounded-xl border border-runtime-line-soft/70 bg-runtime-panel/60 px-3.5 py-3">
        <AccessSkeletonBar className="h-4 w-40" />
        <AccessSkeletonBar className="h-3 w-16" />
        <div className="ml-auto flex gap-2">
          <AccessSkeletonBar className="h-3 w-20" />
          <AccessSkeletonBar className="h-3 w-20" />
        </div>
      </div>
      <AccessSkeletonBar className="h-9 w-full max-w-xl" />
      <div className="grid gap-3 md:grid-cols-2">
        {Array.from({ length: 4 }).map((_, index) => (
          <SurfacePanel key={index} as="div" className="p-4">
            <AccessSkeletonBar className="h-4 w-24" />
            <AccessSkeletonBar className="mt-2 h-5 w-20" />
            <AccessSkeletonBar className="mt-3 h-3 w-full" />
            <AccessSkeletonBar className="mt-1.5 h-3 w-2/3" />
          </SurfacePanel>
        ))}
      </div>
    </div>
  );
}

function AccessSkeletonBar({ className = "" }: { className?: string }) {
  return (
    <span
      aria-hidden="true"
      className={`block animate-pulse rounded bg-runtime-line-soft/60 ${className}`}
    />
  );
}

function AccessOverview({ data }: { data: ServiceAccessData }) {
  const posture = accessPosture(data);
  const nextAction = accessNextAction(data);

  return (
    <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_minmax(300px,380px)]">
      <div className="grid gap-3 md:grid-cols-2">
        <AccessOverviewCard
          title="Langfuse"
          href="/access/langfuse"
          status={statusSummary(data, "langfuse")}
          tone={serviceTone(data, "langfuse")}
          description="Observability logins, project links, public keys, and secret refs."
        />
        <AccessOverviewCard
          title="LiteLLM"
          href="/access/litellm"
          status={statusSummary(data, "litellm")}
          tone={serviceTone(data, "litellm")}
          description="Gateway base URLs, teams, key refs, and provisioning errors."
        />
        <AccessOverviewCard
          title="Gitea orgs"
          href="/access/gitea"
          status={statusSummary(data, "gitea")}
          tone={serviceTone(data, "gitea")}
          description="Managed source-control organizations and account-level access."
        />
        <AccessOverviewCard
          title="Repositories"
          href="/access/repositories"
          status={`${data.gitea.repositories.length} repos`}
          tone={data.gitea.repositories.length > 0 ? "emerald" : "neutral"}
          description="Agent repositories, visibility state, source links, and agent links."
        />
      </div>

      <aside className="min-w-0">
        <div className="text-[10px] font-medium uppercase tracking-wide text-ink-faint">
          Admin path
        </div>
        <h2 className="mt-2 text-base font-semibold text-ink">
          Keep access ready
        </h2>
        <div className="mt-4 grid gap-2">
          <AccessActionLink
            href={nextAction.href}
            label={nextAction.label}
            detail={nextAction.detail}
            tone={nextAction.tone === "primary" ? "amber" : posture.tone}
          />
          <AccessActionLink
            href="/access/langfuse"
            label="Review observability"
            detail={statusSummary(data, "langfuse")}
            tone={serviceTone(data, "langfuse")}
          />
          <AccessActionLink
            href="/access/litellm"
            label="Review model gateway"
            detail={statusSummary(data, "litellm")}
            tone={serviceTone(data, "litellm")}
          />
          <AccessActionLink
            href="/access/repositories"
            label="Review source links"
            detail={`${data.gitea.repositories.length.toLocaleString()} agent repos in inventory.`}
            tone={data.gitea.repositories.length > 0 ? "emerald" : "neutral"}
          />
        </div>
        {posture.errorCount > 0 && (
          <InlineAlert tone="amber" className="mt-4">
            {posture.errorCount} service record{posture.errorCount === 1 ? "" : "s"} reported provisioning errors.
          </InlineAlert>
        )}
      </aside>
    </div>
  );
}

function AccessOverviewCard({
  title,
  href,
  status,
  tone,
  description,
}: {
  title: string;
  href: string;
  status: string;
  tone: "neutral" | "emerald" | "amber" | "red";
  description: string;
}) {
  return (
    <SelectableSurfaceLink href={href} className="p-4">
      <div className="flex min-w-0 items-start justify-between gap-3">
        <div className="min-w-0">
          <h2 className="text-base font-semibold text-ink">{title}</h2>
          <div className="mt-2 min-w-0">
            <StatusBadge tone={tone} className="max-w-full">
              <span className="truncate">{status}</span>
            </StatusBadge>
          </div>
        </div>
        <span className="shrink-0 text-xs font-medium text-ink-dim">
          Open
        </span>
      </div>
      <p className="mt-3 text-sm leading-relaxed text-ink-muted">
        {description}
      </p>
    </SelectableSurfaceLink>
  );
}

function AccessActionLink({
  href,
  label,
  detail,
  tone = "neutral",
}: {
  href: string;
  label: string;
  detail: string;
  tone?: "neutral" | "emerald" | "amber" | "red";
}) {
  const dotClassName =
    tone === "emerald"
      ? "bg-signal-live"
      : tone === "amber"
        ? "bg-signal-authority"
        : tone === "red"
          ? "bg-signal-danger"
          : "bg-ink-faint";

  return (
    <SelectableSurfaceLink href={href} className="p-3">
      <div className="flex items-start gap-3">
        <span
          aria-hidden="true"
          className={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${dotClassName}`}
        />
        <span className="min-w-0">
          <span className="block truncate text-sm font-medium text-ink">
            {label}
          </span>
          <span className="mt-1 block text-xs leading-relaxed text-ink-muted">
            {detail || "No status reported."}
          </span>
        </span>
      </div>
    </SelectableSurfaceLink>
  );
}

function LangfuseSection({
  data,
  selectedOrgSlug,
  onCloseDetail,
}: {
  data: ServiceAccessData;
  selectedOrgSlug: string | null;
  onCloseDetail: () => void;
}) {
  const selectedOrg = selectedOrgSlug
    ? data.organizations.find((org) => org.slug === selectedOrgSlug) ?? null
    : null;

  return (
    <div data-onboarding-target="access-langfuse">
      <SectionPanel
        title="Langfuse"
        description="Org-scoped observability projects and login state."
      >
        {data.organizations.length === 0 ? (
          <EmptyState title="No organizations" />
        ) : (
          <div className="grid gap-2">
            {data.organizations.map((org) => (
              <LangfuseOrgRow
                key={org.id}
                org={org}
                selected={org.slug === selectedOrgSlug}
              />
            ))}
          </div>
        )}
      </SectionPanel>

      {/* Mandate C: org detail opens in-place as a right-side sheet; the org
          list above stays mounted and visible behind it. */}
      <DetailSheet
        open={selectedOrgSlug !== null}
        onClose={onCloseDetail}
        size="lg"
        title={
          <span className="[overflow-wrap:anywhere]">
            {selectedOrg ? selectedOrg.name : selectedOrgSlug ?? "Langfuse access"}
          </span>
        }
        description="Langfuse access"
      >
        {selectedOrgSlug !== null && (
          <LangfuseOrgDetail org={selectedOrg} selectedOrgSlug={selectedOrgSlug} />
        )}
      </DetailSheet>
    </div>
  );
}

function LiteLlmSection({
  data,
  selectedOrgSlug,
  onCloseDetail,
}: {
  data: ServiceAccessData;
  selectedOrgSlug: string | null;
  onCloseDetail: () => void;
}) {
  const selectedOrg = selectedOrgSlug
    ? data.organizations.find((org) => org.slug === selectedOrgSlug) ?? null
    : null;

  return (
    <div data-onboarding-target="access-litellm">
      <SectionPanel
        title="LiteLLM"
        description="OpenAI-compatible gateway routing used by chat, agents, proofs, trials, and deployments."
      >
        {data.organizations.length === 0 ? (
          <EmptyState title="No gateway scopes" />
        ) : (
          <div className="grid gap-2">
            {data.organizations.map((org) => (
              <LiteLlmOrgRow
                key={org.id}
                org={org}
                selected={org.slug === selectedOrgSlug}
              />
            ))}
          </div>
        )}
      </SectionPanel>

      <DetailSheet
        open={selectedOrgSlug !== null}
        onClose={onCloseDetail}
        size="lg"
        title={
          <span className="[overflow-wrap:anywhere]">
            {selectedOrg ? selectedOrg.name : selectedOrgSlug ?? "LiteLLM access"}
          </span>
        }
        description="LiteLLM access"
      >
        {selectedOrgSlug !== null && (
          <LiteLlmOrgDetail org={selectedOrg} selectedOrgSlug={selectedOrgSlug} />
        )}
      </DetailSheet>
    </div>
  );
}

function GiteaSection({
  data,
  selectedOrgSlug,
  onCloseDetail,
}: {
  data: ServiceAccessData;
  selectedOrgSlug: string | null;
  onCloseDetail: () => void;
}) {
  const selectedOrg = selectedOrgSlug
    ? data.organizations.find((org) => org.slug === selectedOrgSlug) ?? null
    : null;

  return (
    <div data-onboarding-target="access-gitea">
      <SectionPanel
        title="Gitea orgs"
        description="Managed source-control organizations and base account access."
        actions={<ExternalLink href={data.gitea.base_url}>Open Gitea</ExternalLink>}
      >
        <div className="grid gap-3 border-b border-runtime-line-soft/60 pb-4 text-sm md:grid-cols-3">
          <ReadOnlyValueField label="URL" value={data.gitea.base_url} copyable />
          <ReadOnlyValueField label="User" value={data.gitea.username} copyable />
          <ReadOnlyValueField label="Auth" value={authModeLabel(data.gitea.auth_mode)} />
        </div>
        {data.organizations.length === 0 ? (
          <div className="pt-4">
            <EmptyState title="No organizations" />
          </div>
        ) : (
          <div className="grid gap-2 pt-4">
            {data.organizations.map((org) => (
              <GiteaOrgRow
                key={org.id}
                org={org}
                selected={org.slug === selectedOrgSlug}
              />
            ))}
          </div>
        )}
      </SectionPanel>

      <DetailSheet
        open={selectedOrgSlug !== null}
        onClose={onCloseDetail}
        size="lg"
        title={
          <span className="[overflow-wrap:anywhere]">
            {selectedOrg ? selectedOrg.name : selectedOrgSlug ?? "Gitea access"}
          </span>
        }
        description="Gitea access"
      >
        {selectedOrgSlug !== null && (
          <GiteaOrgDetail
            data={data}
            org={selectedOrg}
            selectedOrgSlug={selectedOrgSlug}
          />
        )}
      </DetailSheet>
    </div>
  );
}

function RepositoriesSection({
  data,
  selectedAgentName,
  onCloseDetail,
}: {
  data: ServiceAccessData;
  selectedAgentName: string | null;
  onCloseDetail: () => void;
}) {
  const selectedRepo = selectedAgentName
    ? data.gitea.repositories.find((repo) => repo.agent_name === selectedAgentName) ?? null
    : null;

  return (
    <div data-onboarding-target="access-repositories">
      <SectionPanel
        title="Repositories"
        description="Source repositories created for your agents."
        actions={<ExternalLink href={data.gitea.base_url}>Open Gitea</ExternalLink>}
      >
        {data.gitea.repositories.length === 0 ? (
          <EmptyState title="No repositories" />
        ) : (
          <div className="grid gap-2">
            {data.gitea.repositories.map((repo) => (
              <GiteaRepoRow
                key={repo.agent_name}
                repo={repo}
                selected={repo.agent_name === selectedAgentName}
              />
            ))}
          </div>
        )}
      </SectionPanel>

      <DetailSheet
        open={selectedAgentName !== null}
        onClose={onCloseDetail}
        size="lg"
        title={
          <span className="font-mono [overflow-wrap:anywhere]">
            {selectedAgentName ?? "Repository detail"}
          </span>
        }
        description="Repository detail"
      >
        {selectedAgentName !== null && (
          <GiteaRepoDetail repo={selectedRepo} selectedAgentName={selectedAgentName} />
        )}
      </DetailSheet>
    </div>
  );
}

type AccessServiceKey = "langfuse" | "litellm" | "gitea";
type AccessTone = "neutral" | "emerald" | "amber" | "red";

type AccessServiceCheck = {
  serviceKey: AccessServiceKey;
  orgSlug: string;
  ready: boolean;
  status: string;
  href: string;
  lastError: string | null;
};

const readyAccessStatuses = new Set([
  "active",
  "available",
  "configured",
  "enabled",
  "healthy",
  "ok",
  "provisioned",
  "ready",
  "succeeded",
  "success",
]);

function accessPosture(data: ServiceAccessData) {
  const checks = accessServiceChecks(data);
  const serviceCount = checks.length;
  const readyCount = checks.filter((check) => check.ready).length;
  const errorCount = checks.filter((check) => check.lastError).length;
  const tone: AccessTone =
    errorCount > 0
      ? "amber"
      : serviceCount > 0 && readyCount === serviceCount
        ? "emerald"
        : serviceCount === 0
          ? "neutral"
          : "amber";
  const label =
    serviceCount === 0
      ? "no orgs"
      : errorCount > 0
        ? "needs review"
        : readyCount === serviceCount
          ? "ready"
          : "partial";
  const detail =
    serviceCount === 0
      ? "create an organization to provision managed services"
      : `${readyCount} of ${serviceCount} service checks ready`;

  return { checks, detail, errorCount, label, readyCount, serviceCount, tone };
}

function accessNextAction(data: ServiceAccessData) {
  const posture = accessPosture(data);
  const errorCheck = posture.checks.find((check) => check.lastError);
  if (data.organizations.length === 0) {
    return {
      label: "Create or select an organization",
      detail: "Managed Langfuse, LiteLLM, and Gitea org access appears after an organization exists.",
      href: "/organization",
      action: "Organization",
      tone: "primary" as const,
    };
  }
  if (errorCheck) {
    return {
      label: `Review ${serviceLabel(errorCheck.serviceKey)} provisioning`,
      detail: errorCheck.lastError || "A managed service reported an error.",
      href: errorCheck.href,
      action: "Review error",
      tone: "primary" as const,
    };
  }
  const pendingCheck = posture.checks.find((check) => !check.ready);
  if (pendingCheck) {
    return {
      label: `Review pending ${serviceLabel(pendingCheck.serviceKey)} access`,
      detail: `${pendingCheck.orgSlug} is reporting ${authModeLabel(pendingCheck.status)}.`,
      href: pendingCheck.href,
      action: "Review status",
      tone: "secondary" as const,
    };
  }
  if (data.gitea.repositories.length === 0) {
    return {
      label: "Check repository coverage",
      detail: "No agent repositories are currently listed in the access inventory.",
      href: "/access/repositories",
      action: "Repositories",
      tone: "secondary" as const,
    };
  }
  return {
    label: "Review repository inventory",
    detail: `${data.gitea.repositories.length.toLocaleString()} agent repositories are connected to managed source control.`,
    href: "/access/repositories",
    action: "Review repos",
    tone: "secondary" as const,
  };
}

function accessServiceChecks(data: ServiceAccessData): AccessServiceCheck[] {
  return data.organizations.flatMap((org) => [
    {
      serviceKey: "langfuse" as const,
      orgSlug: org.slug,
      ready: isAccessStatusReady(org.langfuse.status) && Boolean(org.langfuse.project_url),
      status: org.langfuse.status,
      href: accessLangfuseOrgRoute(org.slug),
      lastError: org.langfuse.last_error,
    },
    {
      serviceKey: "litellm" as const,
      orgSlug: org.slug,
      ready: isAccessStatusReady(org.litellm.status) && org.litellm.key_configured,
      status: org.litellm.status,
      href: accessLiteLlmOrgRoute(org.slug),
      lastError: org.litellm.last_error,
    },
    {
      serviceKey: "gitea" as const,
      orgSlug: org.slug,
      ready: isAccessStatusReady(org.gitea.status) && Boolean(org.gitea.org_url),
      status: org.gitea.status,
      href: accessGiteaOrgRoute(org.slug),
      lastError: org.gitea.last_error,
    },
  ]);
}

function serviceTone(data: ServiceAccessData, serviceKey: AccessServiceKey): AccessTone {
  const checks = accessServiceChecks(data).filter((check) => check.serviceKey === serviceKey);
  if (checks.length === 0) return "neutral";
  if (checks.some((check) => check.lastError)) return "amber";
  if (checks.every((check) => check.ready)) return "emerald";
  return "amber";
}

function isAccessStatusReady(status: string) {
  return readyAccessStatuses.has(String(status || "").trim().toLowerCase());
}

function serviceLabel(serviceKey: AccessServiceKey) {
  if (serviceKey === "litellm") return "LiteLLM";
  if (serviceKey === "gitea") return "Gitea";
  return "Langfuse";
}

function statusSummary(data: ServiceAccessData, serviceKey: AccessServiceKey) {
  if (data.organizations.length === 0) return "no orgs";
  const counts = data.organizations.reduce<Record<string, number>>((acc, org) => {
    const status = org[serviceKey].status || "unknown";
    acc[status] = (acc[status] || 0) + 1;
    return acc;
  }, {});
  return Object.entries(counts)
    .map(([status, count]) => `${count} ${authModeLabel(status)}`)
    .join(", ");
}

function LangfuseOrgRow({
  org,
  selected = false,
  compact = false,
}: {
  org: OrganizationServiceAccess;
  selected?: boolean;
  compact?: boolean;
}) {
  const service = org.langfuse;
  return (
    <SelectableSurfaceLink
      href={accessLangfuseOrgRoute(org.slug)}
      selected={selected}
      className="p-3 text-xs"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <OrgLabel org={org} status={service.status} />
        <div className="text-[11px] text-ink-soft">
          Open
        </div>
      </div>
      <div className="mt-2 flex flex-wrap gap-2 text-xs text-ink-muted">
        <span>{service.account_status}</span>
        <span>{service.project_name || "project pending"}</span>
        {!compact && <span>{service.login_email}</span>}
      </div>
    </SelectableSurfaceLink>
  );
}

function LangfuseOrgDetail({
  org,
  selectedOrgSlug,
}: {
  org: OrganizationServiceAccess | null;
  selectedOrgSlug: string;
}) {
  if (!org) {
    return (
      <AccessDetailMissing
        slug={selectedOrgSlug}
        message="This organization is not in the current access inventory."
      />
    );
  }

  const service = org.langfuse;

  return (
    <div className="min-w-0">
      <div className="flex flex-wrap items-center gap-2 border-b border-runtime-line-soft/60 pb-3">
        <StateBadge status={service.status} />
        <span className="font-mono text-xs text-ink-muted">{org.slug}</span>
        <div className="ml-auto flex flex-wrap gap-2">
          <ExternalLink href={service.login_url}>Login</ExternalLink>
          {service.project_url && (
            <ExternalLink href={service.project_url}>Project</ExternalLink>
          )}
        </div>
      </div>

      <div className="grid gap-4 pt-4">
        {service.last_error && (
          <InlineAlert tone="amber" className="text-xs">
            {service.last_error}
          </InlineAlert>
        )}
        <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-4">
          <SummaryMetric label="service" value={service.status} size="compact" />
          <SummaryMetric label="account" value={service.account_status} size="compact" />
          <SummaryMetric label="role" value={service.role || "pending"} size="compact" />
          <SummaryMetric
            label="provisioned"
            value={formatAccessDate(service.provisioned_at)}
            size="compact"
            mono={false}
          />
        </div>

        <SectionPanel title="Login">
          <div className="grid gap-2 lg:grid-cols-2">
            <ReadOnlyValueField label="Base URL" value={service.base_url} copyable />
            <ReadOnlyValueField label="Login URL" value={service.login_url} copyable />
            <ReadOnlyValueField label="Email" value={service.login_email} copyable />
            <ReadOnlyValueField
              label="Password"
              value={service.login_password || "pending"}
              copyable
            />
            <ReadOnlyValueField
              label="Password ref"
              value={service.login_password_ref || "pending"}
              copyable
            />
          </div>
        </SectionPanel>

        <SectionPanel title="Project">
          <div className="grid gap-2 lg:grid-cols-2">
            <ReadOnlyValueField
              label="Project"
              value={service.project_name || "pending"}
              copyable
            />
            <ReadOnlyValueField
              label="Project id"
              value={service.project_id || "pending"}
              copyable
            />
            <ReadOnlyValueField
              label="Project URL"
              value={service.project_url || "pending"}
              copyable
            />
            <ReadOnlyValueField
              label="Public key"
              value={service.public_key || "pending"}
              copyable
            />
            <ReadOnlyValueField
              label="Secret ref"
              value={service.secret_key_ref || "pending"}
              copyable
            />
          </div>
        </SectionPanel>
      </div>
    </div>
  );
}

function LiteLlmOrgRow({
  org,
  selected = false,
  compact = false,
}: {
  org: OrganizationServiceAccess;
  selected?: boolean;
  compact?: boolean;
}) {
  const service = org.litellm;
  return (
    <SelectableSurfaceLink
      href={accessLiteLlmOrgRoute(org.slug)}
      selected={selected}
      className="p-3 text-xs"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <OrgLabel org={org} status={service.status} />
        <div className="text-[11px] text-ink-soft">
          Open
        </div>
      </div>
      <div className="mt-2 flex flex-wrap gap-2 text-xs text-ink-muted">
        <span>{service.team_id || "team pending"}</span>
        <span>{service.key_configured ? "key configured" : "key pending"}</span>
        {!compact && (
          <span className="font-mono [overflow-wrap:anywhere]">
            {service.openai_base_url}
          </span>
        )}
      </div>
    </SelectableSurfaceLink>
  );
}

function LiteLlmOrgDetail({
  org,
  selectedOrgSlug,
}: {
  org: OrganizationServiceAccess | null;
  selectedOrgSlug: string;
}) {
  if (!org) {
    return (
      <AccessDetailMissing
        slug={selectedOrgSlug}
        message="This organization is not in the current gateway inventory."
      />
    );
  }

  const service = org.litellm;

  return (
    <div className="min-w-0">
      <div className="flex flex-wrap items-center gap-2 border-b border-runtime-line-soft/60 pb-3">
        <StateBadge status={service.status} />
        <span className="font-mono text-xs text-ink-muted">{org.slug}</span>
      </div>

      <div className="grid gap-4 pt-4">
        {service.last_error && (
          <InlineAlert tone="amber" className="text-xs">
            {service.last_error}
          </InlineAlert>
        )}
        <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-4">
          <SummaryMetric label="service" value={service.status} size="compact" />
          <SummaryMetric
            label="key"
            value={service.key_configured ? "configured" : "pending"}
            size="compact"
          />
          <SummaryMetric
            label="team"
            value={service.team_id || "pending"}
            size="compact"
          />
          <SummaryMetric label="role" value={org.role} size="compact" />
        </div>

        <SectionPanel title="Gateway endpoints">
          <div className="grid gap-2 lg:grid-cols-2">
            <ReadOnlyValueField label="Base URL" value={service.base_url} copyable />
            <ReadOnlyValueField
              label="OpenAI base URL"
              value={service.openai_base_url}
              copyable
            />
            <ReadOnlyValueField
              label="Team"
              value={service.team_id || "pending"}
              copyable
            />
            <ReadOnlyValueField
              label="Key ref"
              value={service.key_ref || "pending"}
              copyable
            />
            <ReadOnlyValueField
              label="Key"
              value={service.key_configured ? "configured" : "pending"}
            />
          </div>
        </SectionPanel>
      </div>
    </div>
  );
}

function GiteaOrgRow({
  org,
  selected = false,
  compact = false,
}: {
  org: OrganizationServiceAccess;
  selected?: boolean;
  compact?: boolean;
}) {
  const service = org.gitea;
  return (
    <SelectableSurfaceLink
      href={accessGiteaOrgRoute(org.slug)}
      selected={selected}
      className="p-3 text-xs"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <OrgLabel org={org} status={service.status} />
        <div className="text-[11px] text-ink-soft">
          Open
        </div>
      </div>
      <div className="mt-2 flex flex-wrap gap-2 text-xs text-ink-muted">
        <span>{service.org_name || "org pending"}</span>
        <span>{formatAccessDate(service.provisioned_at)}</span>
        {!compact && service.org_url && (
          <span className="font-mono [overflow-wrap:anywhere]">
            {service.org_url}
          </span>
        )}
      </div>
    </SelectableSurfaceLink>
  );
}

function GiteaOrgDetail({
  data,
  org,
  selectedOrgSlug,
}: {
  data: ServiceAccessData;
  org: OrganizationServiceAccess | null;
  selectedOrgSlug: string;
}) {
  if (!org) {
    return (
      <AccessDetailMissing
        slug={selectedOrgSlug}
        message="This organization is not in the current source-control inventory."
      />
    );
  }

  const service = org.gitea;

  return (
    <div className="min-w-0">
      <div className="flex flex-wrap items-center gap-2 border-b border-runtime-line-soft/60 pb-3">
        <StateBadge status={service.status} />
        <span className="font-mono text-xs text-ink-muted">{org.slug}</span>
        <div className="ml-auto flex flex-wrap gap-2">
          <ExternalLink href={data.gitea.base_url}>Gitea</ExternalLink>
          {service.org_url && <ExternalLink href={service.org_url}>Org</ExternalLink>}
        </div>
      </div>

      <div className="grid gap-4 pt-4">
        {service.last_error && (
          <InlineAlert tone="amber" className="text-xs">
            {service.last_error}
          </InlineAlert>
        )}
        <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-4">
          <SummaryMetric label="service" value={service.status} size="compact" />
          <SummaryMetric
            label="managed org"
            value={service.org_name || "pending"}
            size="compact"
          />
          <SummaryMetric label="role" value={org.role} size="compact" />
          <SummaryMetric
            label="provisioned"
            value={formatAccessDate(service.provisioned_at)}
            size="compact"
            mono={false}
          />
        </div>

        <SectionPanel title="Source-control account">
          <div className="grid gap-2 lg:grid-cols-3">
            <ReadOnlyValueField label="Base URL" value={data.gitea.base_url} copyable />
            <ReadOnlyValueField label="User" value={data.gitea.username} copyable />
            <ReadOnlyValueField
              label="Auth"
              value={authModeLabel(data.gitea.auth_mode)}
            />
          </div>
        </SectionPanel>

        <SectionPanel title="Managed organization">
          <div className="grid gap-2 lg:grid-cols-2">
            <ReadOnlyValueField
              label="Org"
              value={service.org_name || "pending"}
              copyable
            />
            <ReadOnlyValueField
              label="Org URL"
              value={service.org_url || "pending"}
              copyable
            />
            <ReadOnlyValueField label="Account role" value={org.role} />
            <ReadOnlyValueField
              label="Provisioned"
              value={formatAccessDate(service.provisioned_at)}
            />
          </div>
        </SectionPanel>
      </div>
    </div>
  );
}

function GiteaRepoRow({
  repo,
  selected = false,
  compact = false,
}: {
  repo: GiteaRepositoryAccess;
  selected?: boolean;
  compact?: boolean;
}) {
  return (
    <SelectableSurfaceLink
      href={accessRepositoryRoute(repo.agent_name)}
      selected={selected}
      className="p-3 text-xs"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="min-w-0">
          <div className="truncate font-mono text-sm text-ink">{repo.agent_name}</div>
          <div className="mt-1 flex flex-wrap items-center gap-2">
            <StateBadge status={repo.status} />
            <span className="text-xs text-ink-muted">
              {repo.public ? "public" : "private"}
            </span>
            {repo.owner && (
              <span className="font-mono text-xs text-ink-muted">{repo.owner}</span>
            )}
          </div>
          {!compact && (
            <div className="mt-1 truncate font-mono text-[11px] text-ink-muted">
              {repo.repo_url}
            </div>
          )}
        </div>
        <div className="text-[11px] text-ink-soft">
          Open
        </div>
      </div>
    </SelectableSurfaceLink>
  );
}

function GiteaRepoDetail({
  repo,
  selectedAgentName,
}: {
  repo: GiteaRepositoryAccess | null;
  selectedAgentName: string;
}) {
  if (!repo) {
    return (
      <AccessDetailMissing
        slug={selectedAgentName}
        message="This repository is not in the current access inventory."
      />
    );
  }

  return (
    <div className="min-w-0">
      <div className="flex flex-wrap items-center gap-2 border-b border-runtime-line-soft/60 pb-3">
        <StateBadge status={repo.status} />
        <span className="text-xs text-ink-muted">
          {repo.public ? "public repository" : "private repository"}
        </span>
        <div className="ml-auto flex flex-wrap gap-2">
          <ExternalLink href={repo.repo_url}>Repo</ExternalLink>
          {repo.agent_url && <ExternalLink href={repo.agent_url}>Agent</ExternalLink>}
        </div>
      </div>

      <div className="grid gap-4 pt-4">
        <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-4">
          <SummaryMetric label="status" value={repo.status} size="compact" />
          <SummaryMetric
            label="visibility"
            value={repo.public ? "public" : "private"}
            size="compact"
          />
          <SummaryMetric label="owner" value={repo.owner || "unknown"} size="compact" />
          <SummaryMetric label="agent" value={repo.agent_name} size="compact" />
        </div>

        <SectionPanel title="Repository links">
          <div className="grid gap-2 lg:grid-cols-2">
            <ReadOnlyValueField label="Repository" value={repo.repo_url} copyable />
            <ReadOnlyValueField
              label="Agent URL"
              value={repo.agent_url || "not published"}
              copyable
            />
          </div>
        </SectionPanel>
      </div>
    </div>
  );
}

function AccessDetailMissing({
  slug,
  message,
}: {
  slug: string;
  message: string;
}) {
  return (
    <div className="min-w-0">
      <h3 className="font-mono text-sm text-ink [overflow-wrap:anywhere]">{slug}</h3>
      <InlineAlert tone="amber" className="mt-3 text-xs">
        {message}
      </InlineAlert>
    </div>
  );
}

function OrgLabel({
  org,
  status,
}: {
  org: OrganizationServiceAccess;
  status: string;
}) {
  return (
    <div className="min-w-0">
      <div className="truncate text-sm font-medium text-ink">{org.name}</div>
      <div className="mt-1 flex flex-wrap items-center gap-2">
        <span className="font-mono text-xs text-ink-muted">{org.slug}</span>
        <span className="text-xs text-ink-faint">{org.role}</span>
        <StateBadge status={status} />
      </div>
    </div>
  );
}

function ExternalLink({
  href,
  children,
}: {
  href: string;
  children: string;
}) {
  return (
    <ToolbarLink
      href={href}
      external
    >
      {children}
    </ToolbarLink>
  );
}

function authModeLabel(value: string) {
  if (value === "platform_managed") return "platform managed";
  return value.replace(/_/g, " ");
}

function formatAccessDate(value: string | null) {
  if (!value) return "pending";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
  });
}
