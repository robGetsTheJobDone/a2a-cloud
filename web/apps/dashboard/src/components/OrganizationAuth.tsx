import { InlineAlert, SurfacePanel, ToolbarLink } from "./DashboardChrome";
import { Icon } from "./Icon";
import { RoutePageShell } from "./RoutePageShell";
import { OrganizationDetailView } from "./organization-auth/OrganizationDetail";
import { OrganizationList } from "./organization-auth/OrganizationList";
import { OrganizationPosture } from "./organization-auth/OrganizationPosture";
import {
  KEYCLOAK_ACCOUNT_URL,
  KEYCLOAK_ADMIN_URL,
} from "./organization-auth/shared";
import { useOrgAuthData } from "./organization-auth/useOrgAuthData";

/**
 * OrganizationAuth — org/auth admin surface. Thin composer over the focused
 * pieces in ./organization-auth: useOrgAuthData (data flow + mutations),
 * OrganizationList (rail), OrganizationPosture (header), and
 * OrganizationDetailView (per-view panels whose detail/edit open as in-place
 * right-side sheets, mandate C). Behavior-preserving decomposition.
 */
export function OrganizationAuth() {
  const data = useOrgAuthData();
  const {
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
  } = data;

  return (
    <RoutePageShell
      routeId="organization"
      actions={
        <div className="flex flex-wrap gap-2">
          <ExternalButton href={KEYCLOAK_ACCOUNT_URL} icon="user">
            Account settings
          </ExternalButton>
          <ExternalButton href={KEYCLOAK_ADMIN_URL} icon="shield">
            Keycloak console
          </ExternalButton>
        </div>
      }
    >
      {err && <InlineAlert tone="red">{err}</InlineAlert>}

      <section className="grid gap-5 lg:grid-cols-[300px_1fr]">
        <OrganizationList
          orgs={orgs}
          loading={loading}
          routeOrgSlug={routeOrgSlug}
          newOrgName={newOrgName}
          newOrgSlug={newOrgSlug}
          busy={busy}
          onNewOrgName={setNewOrgName}
          onNewOrgSlug={setNewOrgSlug}
          onCreateOrg={handleCreateOrg}
        />

        {selectedOrg ? (
          <div className="space-y-5">
            <OrganizationPosture
              org={selectedOrg}
              activeView={activeView.id}
              domains={domains}
              members={members}
              auditLogs={auditLogs}
              scimTokens={scimTokens}
              verifiedDomainCount={verifiedDomainCount}
              activeScimTokenCount={activeScimTokenCount}
            />

            {detailsErr && <InlineAlert tone="red">{detailsErr}</InlineAlert>}
            {detailsLoading ? (
              <SurfacePanel as="div" className="bg-runtime-bg px-4 py-5 text-sm text-ink-muted">
                Loading governance data...
              </SurfacePanel>
            ) : (
              <OrganizationDetailView
                org={selectedOrg}
                view={activeView.id}
                domains={domains}
                members={members}
                auditLogs={auditLogs}
                scimConfig={scimConfig}
                scimTokens={scimTokens}
                selectedDomainName={routeDomainName}
                selectedScimTokenId={routeScimTokenId}
                selectedAuditLogId={routeAuditLogId}
                selectedMemberId={routeMemberId}
                newDomain={newDomain}
                scimLabel={scimLabel}
                busy={busy}
                createdToken={createdToken}
                verifiedDomainCount={verifiedDomainCount}
                activeScimTokenCount={activeScimTokenCount}
                onNewDomain={setNewDomain}
                onAddDomain={handleAddDomain}
                onVerifyDomain={handleVerifyDomain}
                onDeleteDomain={handleDeleteDomain}
                onScimLabel={setScimLabel}
                onCreateScimToken={handleCreateScimToken}
                onRevokeScimToken={handleRevokeScimToken}
              />
            )}
          </div>
        ) : (
          <SurfacePanel as="div" className="bg-runtime-bg px-4 py-5 text-sm text-ink-muted">
            Select or create an organization.
          </SurfacePanel>
        )}
      </section>
    </RoutePageShell>
  );
}

function ExternalButton({
  href,
  icon,
  children,
}: {
  href: string;
  icon: "user" | "shield";
  children: string;
}) {
  return (
    <ToolbarLink href={href} external size="md">
      <Icon name={icon} size={16} />
      {children}
    </ToolbarLink>
  );
}
