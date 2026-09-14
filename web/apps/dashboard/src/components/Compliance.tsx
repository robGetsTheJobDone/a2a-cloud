import { useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  EmptyState,
  FormField,
  InlineAlert,
  LoadingState,
  SelectInput,
  SurfacePanel,
  ToolbarButton,
  ToolbarLink,
} from "./DashboardChrome";
import { RoutePageShell } from "./RoutePageShell";
import { AgentRiskPanel } from "./compliance/AgentRiskPanel";
import { CompliancePosture } from "./compliance/CompliancePosture";
import { DecisionRecordsPanel } from "./compliance/DecisionRecordsPanel";
import { ExportDialog } from "./compliance/ExportDialog";
import { PolicyPanel } from "./compliance/PolicyPanel";
import { complianceRoute } from "./compliance/shared";
import { useComplianceData } from "./compliance/useComplianceData";

/**
 * Compliance — org-scoped EU AI Act readiness surface (Aug 2 2026
 * enforcement). Thin composer over the pieces in ./compliance:
 * useComplianceData (org resolution + cached status/agents resources),
 * CompliancePosture (readiness header + view tabs), and the per-view panels
 * (decision records, policy, agent risk) plus the evidence-pack export dialog.
 */
export function Compliance() {
  const navigate = useNavigate();
  const {
    activeView,
    routeOrgSlug,
    orgs,
    orgsLoading,
    orgsErr,
    selectedOrg,
    isOwner,
    status,
    statusErr,
    statusLoading,
    refreshStatus,
    agents,
    agentsErr,
    agentsLoading,
    refreshAgents,
  } = useComplianceData();
  const [exportOpen, setExportOpen] = useState(false);

  async function handlePolicyChanged() {
    await refreshStatus();
  }

  async function handleAgentSaved() {
    // Classification changes move the classified/high-risk counters too.
    await Promise.all([refreshAgents(), refreshStatus()]);
  }

  return (
    <RoutePageShell
      routeId="compliance"
      actions={
        <div className="flex flex-wrap items-end gap-2">
          {orgs.length > 1 && routeOrgSlug && (
            <FormField label="Organization" className="w-48">
              <SelectInput
                compact
                value={routeOrgSlug}
                onChange={(event) =>
                  navigate(complianceRoute(event.target.value, activeView.id))
                }
              >
                {orgs.map((org) => (
                  <option key={org.slug} value={org.slug}>
                    {org.name}
                  </option>
                ))}
              </SelectInput>
            </FormField>
          )}
          <ToolbarButton
            variant="primary"
            size="md"
            disabled={!selectedOrg}
            onClick={() => setExportOpen(true)}
          >
            Export evidence pack
          </ToolbarButton>
        </div>
      }
    >
      {orgsErr && <InlineAlert tone="red">{orgsErr}</InlineAlert>}

      {orgsLoading && orgs.length === 0 ? (
        <LoadingState label="Loading organizations..." />
      ) : orgs.length === 0 ? (
        <EmptyState
          title="No organization"
          description="Compliance is managed per organization. Create one to start tracking EU AI Act readiness."
          action={
            <ToolbarLink href="/organization" variant="primary" size="md">
              Create an organization
            </ToolbarLink>
          }
        />
      ) : !selectedOrg ? (
        <LoadingState label="Resolving organization..." />
      ) : (
        <section className="space-y-5">
          {statusErr && <InlineAlert tone="red">{statusErr}</InlineAlert>}

          {!status && statusLoading ? (
            <SurfacePanel
              as="div"
              className="bg-runtime-bg px-4 py-5 text-sm text-ink-muted"
            >
              Loading compliance status...
            </SurfacePanel>
          ) : status ? (
            <>
              <CompliancePosture
                org={selectedOrg}
                status={status}
                activeView={activeView.id}
              />

              {activeView.id === "records" && (
                <DecisionRecordsPanel orgSlug={selectedOrg.slug} />
              )}
              {activeView.id === "policy" && (
                <PolicyPanel
                  orgSlug={selectedOrg.slug}
                  policy={status.policy}
                  isOwner={isOwner}
                  onPolicyChanged={handlePolicyChanged}
                />
              )}
              {activeView.id === "agents" && (
                <>
                  {agentsErr && <InlineAlert tone="red">{agentsErr}</InlineAlert>}
                  <AgentRiskPanel
                    orgSlug={selectedOrg.slug}
                    agents={agents}
                    loading={agentsLoading}
                    onSaved={handleAgentSaved}
                  />
                </>
              )}
            </>
          ) : !statusErr ? (
            <SurfacePanel
              as="div"
              className="bg-runtime-bg px-4 py-5 text-sm text-ink-muted"
            >
              Compliance status is unavailable for this organization.
            </SurfacePanel>
          ) : null}
        </section>
      )}

      {selectedOrg && (
        <ExportDialog
          orgSlug={selectedOrg.slug}
          open={exportOpen}
          onClose={() => setExportOpen(false)}
        />
      )}
    </RoutePageShell>
  );
}
