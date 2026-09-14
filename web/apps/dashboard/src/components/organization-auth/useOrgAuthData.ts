import { useCallback, useEffect, useMemo, useState, type FormEvent } from "react";
import { useLocation, useNavigate, useParams } from "react-router-dom";
import {
  addOrganizationDomain,
  createOrganization,
  createOrganizationScimToken,
  deleteOrganizationDomain,
  getOrganizationScimConfig,
  listOrganizationAuditLogs,
  listOrganizationDomains,
  listOrganizationMembers,
  listOrganizationScimTokens,
  listOrganizations,
  revokeOrganizationScimToken,
  verifyOrganizationDomain,
} from "../../api";
import {
  DASHBOARD_SECTION_CACHE_KEYS,
  dashboardScopedCacheKey,
  useDashboardSectionResource,
} from "../DashboardSectionCache";
import {
  decodeRouteSegment,
  organizationViewForPath,
} from "../../navigation";
import { organizationRoute, type OrganizationDetails } from "./shared";

function loadOrganizationDetails(slug: string) {
  return Promise.all([
    listOrganizationDomains(slug),
    listOrganizationMembers(slug),
    listOrganizationAuditLogs(slug, 25),
    getOrganizationScimConfig(slug),
    listOrganizationScimTokens(slug),
  ]).then(([domains, members, auditLogs, scimConfig, scimTokens]) => ({
    domains,
    members,
    auditLogs,
    scimConfig,
    scimTokens,
  }));
}

function emptyOrganizationDetails(): OrganizationDetails {
  return {
    domains: [],
    members: [],
    auditLogs: [],
    scimConfig: null,
    scimTokens: [],
  };
}

/**
 * useOrgAuthData — owns every bit of OrganizationAuth's data flow and mutation
 * state. Behavior-preserving extraction of the original component body: route
 * params, cached resources, the create/add/verify/delete/revoke handlers, and
 * the derived selection. No logic changes.
 */
export function useOrgAuthData() {
  const { pathname } = useLocation();
  const navigate = useNavigate();
  const {
    orgSlug: encodedOrgSlug,
    domainName: encodedDomainName,
    scimTokenId: encodedScimTokenId,
    auditLogId: encodedAuditLogId,
    memberId: encodedMemberId,
  } = useParams<{
    orgSlug?: string;
    domainName?: string;
    scimTokenId?: string;
    auditLogId?: string;
    memberId?: string;
  }>();
  const routeOrgSlug = decodeRouteSegment(encodedOrgSlug);
  const routeDomainName = decodeRouteSegment(encodedDomainName);
  const routeScimTokenId = decodeRouteSegment(encodedScimTokenId);
  const routeAuditLogId = decodeRouteSegment(encodedAuditLogId);
  const routeMemberId = decodeRouteSegment(encodedMemberId);
  const activeView = organizationViewForPath(pathname);
  const {
    data: orgsData,
    error: orgsLoadErr,
    loading,
    setData: setOrganizationsData,
  } = useDashboardSectionResource(
    DASHBOARD_SECTION_CACHE_KEYS.settings.organizationList,
    listOrganizations,
  );
  const orgs = orgsData ?? [];
  const loadSelectedOrganizationDetails = useCallback(() => {
    return routeOrgSlug
      ? loadOrganizationDetails(routeOrgSlug)
      : Promise.resolve(emptyOrganizationDetails());
  }, [routeOrgSlug]);
  const {
    data: detailsData,
    error: detailsLoadErr,
    loading: detailsLoading,
    refresh: refreshSelectedDetails,
    setData: setSelectedDetails,
  } = useDashboardSectionResource(
    dashboardScopedCacheKey(
      DASHBOARD_SECTION_CACHE_KEYS.settings.organizationDetailsPrefix,
      routeOrgSlug,
    ),
    loadSelectedOrganizationDetails,
    { enabled: Boolean(routeOrgSlug) },
  );
  const domains = detailsData?.domains ?? [];
  const members = detailsData?.members ?? [];
  const auditLogs = detailsData?.auditLogs ?? [];
  const scimConfig = detailsData?.scimConfig ?? null;
  const scimTokens = detailsData?.scimTokens ?? [];
  const [actionErr, setActionErr] = useState<string | null>(null);
  const [detailsActionErr, setDetailsActionErr] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [newOrgName, setNewOrgName] = useState("");
  const [newOrgSlug, setNewOrgSlug] = useState("");
  const [newDomain, setNewDomain] = useState("");
  const [scimLabel, setScimLabel] = useState("Okta");
  const [createdToken, setCreatedToken] = useState<string | null>(null);
  const err = actionErr ?? orgsLoadErr;
  const detailsErr = detailsActionErr ?? detailsLoadErr;

  const selectedOrg = useMemo(
    () => orgs.find((org) => org.slug === routeOrgSlug) ?? null,
    [orgs, routeOrgSlug],
  );

  useEffect(() => {
    if (loading || orgs.length === 0) return;
    const target = routeOrgSlug && orgs.some((org) => org.slug === routeOrgSlug)
      ? routeOrgSlug
      : orgs[0].slug;
    if (target !== routeOrgSlug) {
      navigate(organizationRoute(target), { replace: true });
    }
  }, [loading, navigate, orgs, routeOrgSlug]);

  const verifiedDomainCount = domains.filter((domain) => domain.verified_at).length;
  const activeScimTokenCount = scimTokens.filter((token) => token.enabled).length;

  async function refreshDetails() {
    if (!routeOrgSlug) return;
    try {
      const next = await refreshSelectedDetails();
      if (next) setSelectedDetails(next);
      setDetailsActionErr(null);
    } catch (ex) {
      setDetailsActionErr(ex instanceof Error ? ex.message : String(ex));
      throw ex;
    }
  }

  async function refreshOrganizations(selectSlug?: string) {
    const rows = await listOrganizations();
    setOrganizationsData(rows);
    const target = selectSlug ?? routeOrgSlug ?? rows[0]?.slug;
    if (target) navigate(organizationRoute(target, "overview"), { replace: Boolean(!selectSlug) });
  }

  async function handleCreateOrg(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const name = newOrgName.trim();
    if (!name) return;
    setBusy("org:create");
    setActionErr(null);
    try {
      const org = await createOrganization({
        name,
        slug: newOrgSlug.trim() || undefined,
      });
      setNewOrgName("");
      setNewOrgSlug("");
      await refreshOrganizations(org.slug);
    } catch (ex) {
      setActionErr(ex instanceof Error ? ex.message : String(ex));
    } finally {
      setBusy(null);
    }
  }

  async function handleAddDomain(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!routeOrgSlug) return;
    const value = newDomain.trim();
    if (!value) return;
    setBusy("domain:add");
    setDetailsActionErr(null);
    try {
      await addOrganizationDomain(routeOrgSlug, value);
      setNewDomain("");
      await refreshDetails();
    } catch (ex) {
      setDetailsActionErr(ex instanceof Error ? ex.message : String(ex));
    } finally {
      setBusy(null);
    }
  }

  async function handleVerifyDomain(domain: string) {
    if (!routeOrgSlug) return;
    setBusy(`domain:verify:${domain}`);
    setDetailsActionErr(null);
    try {
      await verifyOrganizationDomain(routeOrgSlug, domain);
      await refreshDetails();
    } catch (ex) {
      setDetailsActionErr(ex instanceof Error ? ex.message : String(ex));
    } finally {
      setBusy(null);
    }
  }

  async function handleDeleteDomain(domain: string): Promise<boolean> {
    if (!routeOrgSlug) return false;
    setBusy(`domain:delete:${domain}`);
    setDetailsActionErr(null);
    try {
      await deleteOrganizationDomain(routeOrgSlug, domain);
      await refreshDetails();
      return true;
    } catch (ex) {
      setDetailsActionErr(ex instanceof Error ? ex.message : String(ex));
      return false;
    } finally {
      setBusy(null);
    }
  }

  async function handleCreateScimToken(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!routeOrgSlug) return;
    const label = scimLabel.trim();
    if (!label) return;
    setBusy("scim:create");
    setDetailsActionErr(null);
    setCreatedToken(null);
    try {
      const token = await createOrganizationScimToken(routeOrgSlug, label);
      setCreatedToken(token.token);
      await refreshDetails();
    } catch (ex) {
      setDetailsActionErr(ex instanceof Error ? ex.message : String(ex));
    } finally {
      setBusy(null);
    }
  }

  async function handleRevokeScimToken(tokenId: number): Promise<boolean> {
    if (!routeOrgSlug) return false;
    setBusy(`scim:revoke:${tokenId}`);
    setDetailsActionErr(null);
    try {
      await revokeOrganizationScimToken(routeOrgSlug, tokenId);
      await refreshDetails();
      return true;
    } catch (ex) {
      setDetailsActionErr(ex instanceof Error ? ex.message : String(ex));
      return false;
    } finally {
      setBusy(null);
    }
  }

  return {
    navigate,
    activeView,
    routeOrgSlug,
    routeDomainName,
    routeScimTokenId,
    routeAuditLogId,
    routeMemberId,
    orgs,
    loading,
    selectedOrg,
    domains,
    members,
    auditLogs,
    scimConfig,
    scimTokens,
    verifiedDomainCount,
    activeScimTokenCount,
    err,
    detailsErr,
    detailsLoading,
    busy,
    newOrgName,
    newOrgSlug,
    newDomain,
    scimLabel,
    createdToken,
    setNewOrgName,
    setNewOrgSlug,
    setNewDomain,
    setScimLabel,
    handleCreateOrg,
    handleAddDomain,
    handleVerifyDomain,
    handleDeleteDomain,
    handleCreateScimToken,
    handleRevokeScimToken,
  };
}
