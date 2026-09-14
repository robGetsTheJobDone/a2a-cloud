import { useCallback, useEffect, useMemo } from "react";
import { useLocation, useNavigate, useParams } from "react-router-dom";
import {
  getComplianceStatus,
  listComplianceAgents,
  listOrganizations,
  type ComplianceAgentClassification,
  type ComplianceStatus,
} from "../../api";
import {
  DASHBOARD_SECTION_CACHE_KEYS,
  dashboardScopedCacheKey,
  useDashboardSectionResource,
} from "../DashboardSectionCache";
import { complianceViewForPath, decodeRouteSegment } from "../../navigation";
import { complianceRoute } from "./shared";

/**
 * useComplianceData — owns the Compliance page's data flow: org selection from
 * the route (same slug-redirect behavior as useOrgAuthData), the cached
 * /compliance/status resource, and the cached /compliance/agents resource.
 * Mutations live in the panels; they call the refresh functions returned here.
 */
export function useComplianceData() {
  const { pathname } = useLocation();
  const navigate = useNavigate();
  const { orgSlug: encodedOrgSlug } = useParams<{ orgSlug?: string }>();
  const routeOrgSlug = decodeRouteSegment(encodedOrgSlug);
  const activeView = complianceViewForPath(pathname);

  const {
    data: orgsData,
    error: orgsErr,
    loading: orgsLoading,
  } = useDashboardSectionResource(
    DASHBOARD_SECTION_CACHE_KEYS.settings.organizationList,
    listOrganizations,
  );
  const orgs = orgsData ?? [];
  const selectedOrg = useMemo(
    () => orgs.find((org) => org.slug === routeOrgSlug) ?? null,
    [orgs, routeOrgSlug],
  );

  useEffect(() => {
    if (orgsLoading || orgs.length === 0) return;
    const target = routeOrgSlug && orgs.some((org) => org.slug === routeOrgSlug)
      ? routeOrgSlug
      : orgs[0].slug;
    if (target !== routeOrgSlug) {
      navigate(complianceRoute(target), { replace: true });
    }
  }, [navigate, orgs, orgsLoading, routeOrgSlug]);

  const loadStatus = useCallback(
    (): Promise<ComplianceStatus | null> =>
      routeOrgSlug ? getComplianceStatus(routeOrgSlug) : Promise.resolve(null),
    [routeOrgSlug],
  );
  const {
    data: status,
    error: statusErr,
    loading: statusLoading,
    refresh: refreshStatusResource,
    setData: setStatus,
  } = useDashboardSectionResource(
    dashboardScopedCacheKey(
      DASHBOARD_SECTION_CACHE_KEYS.settings.complianceStatusPrefix,
      routeOrgSlug,
    ),
    loadStatus,
    { enabled: Boolean(routeOrgSlug) },
  );

  const loadAgents = useCallback(
    (): Promise<ComplianceAgentClassification[]> =>
      routeOrgSlug ? listComplianceAgents(routeOrgSlug) : Promise.resolve([]),
    [routeOrgSlug],
  );
  const {
    data: agentsData,
    error: agentsErr,
    loading: agentsLoading,
    refresh: refreshAgentsResource,
    setData: setAgents,
  } = useDashboardSectionResource(
    dashboardScopedCacheKey(
      DASHBOARD_SECTION_CACHE_KEYS.settings.complianceAgentsPrefix,
      routeOrgSlug,
    ),
    loadAgents,
    { enabled: Boolean(routeOrgSlug) },
  );

  const refreshStatus = useCallback(async () => {
    const next = await refreshStatusResource();
    if (next) setStatus(next);
  }, [refreshStatusResource, setStatus]);

  const refreshAgents = useCallback(async () => {
    const next = await refreshAgentsResource();
    if (next) setAgents(next);
  }, [refreshAgentsResource, setAgents]);

  return {
    activeView,
    routeOrgSlug,
    orgs,
    orgsLoading,
    orgsErr,
    selectedOrg,
    isOwner: selectedOrg?.role === "owner",
    status: status ?? null,
    statusErr,
    statusLoading,
    refreshStatus,
    agents: agentsData ?? [],
    agentsErr,
    agentsLoading,
    refreshAgents,
  };
}
