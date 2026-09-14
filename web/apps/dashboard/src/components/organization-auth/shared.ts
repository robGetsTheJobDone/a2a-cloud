import {
  type Organization,
  type OrganizationAuditLog,
  type OrganizationDomain,
  type OrganizationMember,
  type OrganizationScimConfig,
  type OrganizationScimToken,
} from "../../api";
import { type StatusBadgeTone } from "../DashboardChrome";
import { type OrganizationViewId } from "../../navigation";
import { authUrl, keycloakRealm } from "../../lib/platform";

export function keycloakAccountUrl(): string {
  return authUrl(`realms/${keycloakRealm()}/account/`);
}

export function keycloakAdminUrl(): string {
  return authUrl(`admin/${keycloakRealm()}/console/`);
}

export type OrganizationDetails = {
  domains: OrganizationDomain[];
  members: OrganizationMember[];
  auditLogs: OrganizationAuditLog[];
  scimConfig: OrganizationScimConfig | null;
  scimTokens: OrganizationScimToken[];
};

export function organizationRoute(
  slug: string,
  view: OrganizationViewId = "overview",
) {
  const base = `/organization/${encodeURIComponent(slug)}`;
  return view === "overview" ? base : `${base}/${view}`;
}

export function organizationDomainRoute(slug: string, domain: string) {
  return `${organizationRoute(slug, "domains")}/${encodeURIComponent(domain)}`;
}

export function organizationScimTokenRoute(
  slug: string,
  tokenId: number | string,
) {
  return `${organizationRoute(slug, "scim")}/tokens/${encodeURIComponent(String(tokenId))}`;
}

export function organizationAuditLogRoute(
  slug: string,
  logId: number | string,
) {
  return `${organizationRoute(slug, "audit")}/${encodeURIComponent(String(logId))}`;
}

export function organizationMemberRoute(
  slug: string,
  memberId: number | string,
) {
  return `${organizationRoute(slug, "members")}/${encodeURIComponent(String(memberId))}`;
}

export function organizationPosture(
  domains: OrganizationDomain[],
  members: OrganizationMember[],
  auditLogs: OrganizationAuditLog[],
  verifiedDomainCount: number,
  activeScimTokenCount: number,
) {
  const pendingDomainCount = domains.length - verifiedDomainCount;
  const activeMemberCount = members.filter((member) => member.active).length;
  const tone: StatusBadgeTone =
    pendingDomainCount > 0
      ? "amber"
      : members.length > 0 && activeMemberCount === 0
        ? "amber"
        : verifiedDomainCount > 0 || activeScimTokenCount > 0 || auditLogs.length > 0
          ? "emerald"
          : "neutral";
  const label =
    pendingDomainCount > 0
      ? "needs verification"
      : activeMemberCount === 0 && members.length > 0
        ? "member review"
        : tone === "emerald"
          ? "ready"
          : "setup";
  const detail =
    pendingDomainCount > 0
      ? `${pendingDomainCount} domain${pendingDomainCount === 1 ? "" : "s"} pending verification`
      : activeMemberCount === 0 && members.length > 0
        ? "members exist, but none are active"
        : activeScimTokenCount > 0
          ? "SCIM provisioning is configured"
          : verifiedDomainCount > 0
            ? "verified domain coverage is active"
            : "add domains, SCIM, or members to complete governance setup";

  return { activeMemberCount, detail, label, pendingDomainCount, tone };
}

export function organizationNextAction(
  org: Organization,
  domains: OrganizationDomain[],
  members: OrganizationMember[],
  auditLogs: OrganizationAuditLog[],
  verifiedDomainCount: number,
  activeScimTokenCount: number,
) {
  const pendingDomain = domains.find((domain) => !domain.verified_at);
  if (pendingDomain) {
    return {
      label: "Verify pending domain",
      detail: `${pendingDomain.domain} needs DNS verification before it can anchor organization identity.`,
      href: organizationDomainRoute(org.slug, pendingDomain.domain),
      action: "Verify domain",
      tone: "primary" as const,
    };
  }
  if (domains.length === 0) {
    return {
      label: "Add a verified domain",
      detail: "Domains make organization identity and enterprise governance easier to trust.",
      href: organizationRoute(org.slug, "domains"),
      action: "Add domain",
      tone: "primary" as const,
    };
  }
  if (activeScimTokenCount === 0) {
    return {
      label: "Configure SCIM provisioning",
      detail: "Create a SCIM token when the organization is ready for identity-provider sync.",
      href: organizationRoute(org.slug, "scim"),
      action: "SCIM setup",
      tone: "secondary" as const,
    };
  }
  if (!members.some((member) => member.active)) {
    return {
      label: "Review member access",
      detail: "No active members are currently listed for this organization.",
      href: organizationRoute(org.slug, "members"),
      action: "Members",
      tone: "secondary" as const,
    };
  }
  return {
    label: "Review recent governance activity",
    detail: auditLogs.length > 0
      ? `${auditLogs.length.toLocaleString()} recent audit event${auditLogs.length === 1 ? "" : "s"} are available.`
      : `${verifiedDomainCount.toLocaleString()} verified domain${verifiedDomainCount === 1 ? "" : "s"} and SCIM are configured.`,
    href: organizationRoute(org.slug, "audit"),
    action: "Audit log",
    tone: "secondary" as const,
  };
}

export function roleTone(role: string): StatusBadgeTone {
  if (role === "owner") return "emerald";
  if (role === "admin") return "neutral";
  return "neutral";
}

export function auditTargetLabel(log: OrganizationAuditLog) {
  return log.target_email || log.target_id || log.target_type || "unknown target";
}

export function formatDate(value: string | null | undefined) {
  if (!value) return "never";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function formatAuditData(data: Record<string, unknown>) {
  const pairs = Object.entries(data).filter(
    ([, value]) => value !== undefined && value !== null,
  );
  if (pairs.length === 0) return "no metadata";
  return pairs
    .map(([key, value]) => {
      if (typeof value === "object") return `${key}: ${JSON.stringify(value)}`;
      return `${key}: ${String(value)}`;
    })
    .join(" | ");
}
