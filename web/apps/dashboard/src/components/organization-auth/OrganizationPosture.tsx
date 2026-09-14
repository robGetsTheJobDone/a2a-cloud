import {
  type Organization,
  type OrganizationAuditLog,
  type OrganizationDomain,
  type OrganizationMember,
  type OrganizationScimToken,
} from "../../api";
import {
  SegmentedControl,
  SummaryMetric,
  SummaryStrip,
  SurfacePanel,
  TabLink,
  ToolbarLink,
} from "../DashboardChrome";
import { StatusBadge } from "../StatusPillAdapters";
import { ORGANIZATION_VIEWS, type OrganizationViewId } from "../../navigation";
import {
  organizationNextAction,
  organizationPosture,
  organizationRoute,
  roleTone,
} from "./shared";

export function OrganizationPosture({
  org,
  activeView,
  domains,
  members,
  auditLogs,
  scimTokens,
  verifiedDomainCount,
  activeScimTokenCount,
}: {
  org: Organization;
  activeView: OrganizationViewId;
  domains: OrganizationDomain[];
  members: OrganizationMember[];
  auditLogs: OrganizationAuditLog[];
  scimTokens: OrganizationScimToken[];
  verifiedDomainCount: number;
  activeScimTokenCount: number;
}) {
  const posture = organizationPosture(
    domains,
    members,
    auditLogs,
    verifiedDomainCount,
    activeScimTokenCount,
  );
  const nextAction = organizationNextAction(
    org,
    domains,
    members,
    auditLogs,
    verifiedDomainCount,
    activeScimTokenCount,
  );
  const activeMemberCount = members.filter((member) => member.active).length;

  return (
    <SurfacePanel
      as="section"
      data-onboarding-target="organization-posture"
      className="overflow-hidden bg-runtime-bg/80"
    >
      <div className="flex flex-col gap-4 border-b border-runtime-line-soft/70 p-4 sm:p-5 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <StatusBadge tone={posture.tone} dot={posture.tone === "emerald"}>
              {posture.label}
            </StatusBadge>
            <StatusBadge tone={roleTone(org.role)}>{org.role}</StatusBadge>
            <span className="text-xs text-ink-muted">{posture.detail}</span>
          </div>
          <h2 className="mt-3 truncate text-lg font-semibold text-ink">
            {org.name}
          </h2>
          <div className="mt-1 font-mono text-xs text-ink-muted">{org.slug}</div>
        </div>
        <div className="flex shrink-0 flex-wrap gap-2">
          <ToolbarLink href={organizationRoute(org.slug, "domains")} size="md">
            Domains
          </ToolbarLink>
          <ToolbarLink href={organizationRoute(org.slug, "scim")} size="md">
            SCIM
          </ToolbarLink>
        </div>
      </div>

      <div className="grid gap-4 p-4 sm:p-5">
        <SummaryStrip aria-label={`${org.name} governance posture summary`}>
          <SummaryMetric
            label="verified domains"
            value={`${verifiedDomainCount}/${domains.length}`}
            tone={domains.length > verifiedDomainCount ? "amber" : verifiedDomainCount > 0 ? "emerald" : "neutral"}
            detail={domains.length > verifiedDomainCount ? "pending verification" : "identity coverage"}
          />
          <SummaryMetric
            label="members"
            value={members.length.toLocaleString()}
            detail={`${activeMemberCount.toLocaleString()} active`}
          />
          <SummaryMetric
            label="SCIM tokens"
            value={activeScimTokenCount.toLocaleString()}
            tone={activeScimTokenCount > 0 ? "emerald" : "neutral"}
            detail={`${scimTokens.length.toLocaleString()} total`}
          />
          <SummaryMetric
            label="audit events"
            value={auditLogs.length.toLocaleString()}
            detail="recent entries"
          />
        </SummaryStrip>

        <div className="flex flex-col gap-3 border-t border-runtime-line-soft/70 pt-4 lg:flex-row lg:items-center lg:justify-between">
          <div className="min-w-0">
            <div className="text-[10px] font-medium uppercase tracking-wide text-ink-faint">
              Next best action
            </div>
            <div className="mt-1 text-sm font-medium text-ink">
              {nextAction.label}
            </div>
            <p className="mt-1 max-w-2xl text-xs leading-relaxed text-ink-muted">
              {nextAction.detail}
            </p>
          </div>
          <ToolbarLink
            href={nextAction.href}
            variant={nextAction.tone === "primary" ? "primary" : "secondary"}
            size="md"
          >
            {nextAction.action}
          </ToolbarLink>
        </div>
      </div>

      <div className="border-t border-runtime-line-soft/70 px-4 pb-4 sm:px-5 sm:pb-5">
        <SegmentedControl
          role="tablist"
          aria-label={`${org.name} organization views`}
          className="mt-4 flex gap-1 overflow-x-auto bg-runtime-panel/60"
        >
          {ORGANIZATION_VIEWS.map((view) => (
            <TabLink
              key={view.id}
              href={organizationRoute(org.slug, view.id)}
              selected={activeView === view.id}
              className="whitespace-nowrap"
            >
              {view.label}
            </TabLink>
          ))}
        </SegmentedControl>
      </div>
    </SurfacePanel>
  );
}
