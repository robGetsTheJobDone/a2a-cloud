import {
  type Organization,
  type OrganizationAuditLog,
  type OrganizationDomain,
  type OrganizationMember,
} from "../../api";
import {
  InlineAlert,
  SelectableSurfaceLink,
  type StatusBadgeTone,
} from "../DashboardChrome";
import { StatusBadge } from "../StatusPillAdapters";
import {
  organizationNextAction,
  organizationPosture,
  organizationRoute,
} from "./shared";

export function OrganizationOverview({
  org,
  domains,
  members,
  auditLogs,
  verifiedDomainCount,
  activeScimTokenCount,
}: {
  org: Organization;
  domains: OrganizationDomain[];
  members: OrganizationMember[];
  auditLogs: OrganizationAuditLog[];
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

  return (
    <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_minmax(300px,380px)]">
      <div className="grid gap-3 md:grid-cols-2">
        <OrganizationOverviewLink
          href={organizationRoute(org.slug, "domains")}
          label="Domains"
          value={`${verifiedDomainCount}/${domains.length}`}
          detail="verified custom domains"
          tone={domains.length > verifiedDomainCount ? "amber" : verifiedDomainCount > 0 ? "emerald" : "neutral"}
        />
        <OrganizationOverviewLink
          href={organizationRoute(org.slug, "scim")}
          label="SCIM"
          value={activeScimTokenCount.toLocaleString()}
          detail="active provisioning tokens"
          tone={activeScimTokenCount > 0 ? "emerald" : "neutral"}
        />
        <OrganizationOverviewLink
          href={organizationRoute(org.slug, "members")}
          label="Members"
          value={members.length.toLocaleString()}
          detail="active and inactive users"
          tone={members.some((member) => member.active) ? "emerald" : "amber"}
        />
        <OrganizationOverviewLink
          href={organizationRoute(org.slug, "audit")}
          label="Audit"
          value={auditLogs.length.toLocaleString()}
          detail="recent governance events"
          tone="neutral"
        />
      </div>

      <aside className="min-w-0">
        <div className="text-[10px] font-medium uppercase tracking-wide text-ink-faint">
          Governance path
        </div>
        <h2 className="mt-2 text-base font-semibold text-ink">
          Keep identity ready
        </h2>
        <div className="mt-4 grid gap-2">
          <OrganizationActionLink
            href={nextAction.href}
            label={nextAction.label}
            detail={nextAction.detail}
            tone={nextAction.tone === "primary" ? "amber" : posture.tone}
          />
          <OrganizationActionLink
            href={organizationRoute(org.slug, "domains")}
            label="Review domain verification"
            detail={`${verifiedDomainCount} of ${domains.length} domains are verified.`}
            tone={domains.length > verifiedDomainCount ? "amber" : verifiedDomainCount > 0 ? "emerald" : "neutral"}
          />
          <OrganizationActionLink
            href={organizationRoute(org.slug, "scim")}
            label="Review provisioning"
            detail={`${activeScimTokenCount} active SCIM token${activeScimTokenCount === 1 ? "" : "s"}.`}
            tone={activeScimTokenCount > 0 ? "emerald" : "neutral"}
          />
          <OrganizationActionLink
            href={organizationRoute(org.slug, "audit")}
            label="Review recent audit"
            detail={`${auditLogs.length.toLocaleString()} recent governance event${auditLogs.length === 1 ? "" : "s"}.`}
          />
        </div>
        {posture.tone === "amber" && (
          <InlineAlert tone="amber" className="mt-4">
            {posture.detail}
          </InlineAlert>
        )}
      </aside>
    </div>
  );
}

function OrganizationOverviewLink({
  href,
  label,
  value,
  detail,
  tone = "neutral",
}: {
  href: string;
  label: string;
  value: string;
  detail: string;
  tone?: StatusBadgeTone;
}) {
  return (
    <SelectableSurfaceLink
      href={href}
      className="p-4 hover:border-runtime-line-strong hover:bg-runtime-panel/70"
    >
      <div className="flex min-w-0 items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="text-[10px] uppercase tracking-wider text-ink-faint">
            {label}
          </div>
          <div className="mt-2 truncate text-lg font-semibold text-ink">
            {value}
          </div>
        </div>
        <StatusBadge tone={tone} className="shrink-0">
          Open
        </StatusBadge>
      </div>
      <div className="mt-1 truncate text-xs text-ink-muted">
        {detail}
      </div>
    </SelectableSurfaceLink>
  );
}

function OrganizationActionLink({
  href,
  label,
  detail,
  tone = "neutral",
}: {
  href: string;
  label: string;
  detail: string;
  tone?: StatusBadgeTone;
}) {
  const dotClassName =
    tone === "emerald"
      ? "bg-signal-live"
      : tone === "amber"
        ? "bg-signal-authority"
        : tone === "red"
          ? "bg-signal-danger"
          : "bg-ink-muted";

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
            {detail}
          </span>
        </span>
      </div>
    </SelectableSurfaceLink>
  );
}
