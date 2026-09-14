import { publicAgentUrl } from "../../lib/publicAgentUrl";
import { useState } from "react";
import { useLocation } from "react-router-dom";
import {
  disableAgentCodeEditor,
  deleteAgent,
  enableAgentCodeEditor,
  generateOpenApiAgent,
  requestAgentTemplateUpdate,
  refreshAgentCard,
  upgradeAgentRuntime,
  type AgentProofRun,
  type AgentTemplateUpdateResult,
  type MyAgentListing,
  type SubagentRun,
} from "../../api";
import { type AgentDetailSection } from "../../navigation";
import {
  isActiveDeploymentStatus,
  isTransientAgentStatus,
} from "../myAgentsFleet";
import { AgentDeploymentTimeline } from "../AgentDeploymentTimeline";
import { AgentDomainsPanel } from "../AgentDomainsPage";
import { AgentInsightsPanel } from "../AgentInsightsPanel";
import { AgentMailboxPanel } from "../AgentMailboxPanel";
import { AgentProofWorkbench } from "../AgentProofWorkbench";
import { AgentSecretsManager } from "../AgentSecretsManager";
import {
  DefinitionRow,
  InlineAlert,
  SummaryMetric,
  SurfacePanel,
  ToolbarButton,
  ToolbarLink,
} from "../DashboardChrome";
import { StateBadge } from "../StatusPillAdapters";
import {
  type AgentEvidenceView,
  type AgentRunDetailView,
  agentApiUrls,
  fmtDate,
  frontendUrlForAgent,
  openApiSourceUrlsForAgent,
  runtimeUpgradeButtonLabel,
  runtimeUpgradeMessage,
  summarizeRuns,
  templateLineageMiniValue,
} from "./agentTypes";
import { DeploymentPill, RuntimePill } from "./AgentPills";
import { AgentApiAccessPanel, AgentAuthPanel } from "./AgentAuthApi";
import { AgentEvidencePanel } from "./AgentEvidence";
import { AgentRunsPanel } from "./AgentRuns";
import { AgentRuntimePanel } from "./AgentRuntime";

export function MyAgentCard({
  agent,
  activeSection = "overview",
  selectedRunId,
  selectedRunView = "overview",
  selectedCallId,
  selectedProofId,
  selectedDomainHostname,
  evidenceView = "overview",
  onSectionChange,
  runs,
  proofs,
  onProofCreated,
  onAgentUpdated,
  onAgentDeleted,
  onAgentRefreshed,
}: {
  agent: MyAgentListing;
  activeSection?: AgentDetailSection;
  selectedRunId?: string | null;
  selectedRunView?: AgentRunDetailView;
  selectedCallId?: string | null;
  selectedProofId?: string | null;
  selectedDomainHostname?: string | null;
  evidenceView?: AgentEvidenceView;
  onSectionChange: (section: AgentDetailSection) => void;
  runs: SubagentRun[];
  proofs: AgentProofRun[];
  onProofCreated: (proof: AgentProofRun) => void;
  onAgentUpdated: (agent: MyAgentListing) => void;
  onAgentDeleted: (name: string) => void;
  onAgentRefreshed: () => Promise<void>;
}) {
  const location = useLocation();
  const skills = agent.card?.skills || [];
  const runtime = agent.card?.runtime;
  const tools = runtime?.tools_used || [];
  const templateLineage = agent.card?.template_lineage || null;
  const apiUrl = agent.url || (agent.public ? publicAgentUrl(agent.name) : null);
  const appApi = agentApiUrls(agent);
  const frontendUrl = frontendUrlForAgent(agent, apiUrl);
  const runSummary = summarizeRuns(runs);
  const latestProof = proofs[0] || null;
  const upgrade = agent.runtime_upgrade;
  const deployment = agent.latest_deployment;
  const openApiSourceUrls = openApiSourceUrlsForAgent(agent);
  const openApiSourceUrl = openApiSourceUrls[0] || null;
  const deployActive = isActiveDeploymentStatus(deployment?.status);
  const agentDeployActive =
    deployActive || isTransientAgentStatus(agent.status);
  const runtimeUpgradeActive =
    deployment?.trigger === "runtime_upgrade" && isActiveDeploymentStatus(deployment.status);
  const [upgradeBusy, setUpgradeBusy] = useState(false);
  const [upgradeErr, setUpgradeErr] = useState<string | null>(null);
  const [openApiRefreshBusy, setOpenApiRefreshBusy] = useState(false);
  const [openApiRefreshErr, setOpenApiRefreshErr] = useState<string | null>(null);
  const [cardRefreshBusy, setCardRefreshBusy] = useState(false);
  const [cardRefreshErr, setCardRefreshErr] = useState<string | null>(null);
  const [templateUpdateBusy, setTemplateUpdateBusy] = useState(false);
  const [templateUpdateErr, setTemplateUpdateErr] = useState<string | null>(null);
  const [templateUpdateResult, setTemplateUpdateResult] =
    useState<AgentTemplateUpdateResult | null>(null);
  const [codeEditorBusy, setCodeEditorBusy] = useState(false);
  const [codeEditorErr, setCodeEditorErr] = useState<string | null>(null);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [deleteConfirm, setDeleteConfirm] = useState("");
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [deleteErr, setDeleteErr] = useState<string | null>(null);
  const canQueueRuntimeUpgrade =
    Boolean(upgrade?.can_redeploy) && !agentDeployActive && !upgradeBusy;
  const showRuntimeUpgradePanel =
    Boolean(upgrade?.update_available) || runtimeUpgradeActive;
  const codeEditorEnabled = Boolean(agent.code_editor?.enabled);
  const apiAccessOpen = activeSection === "access";
  const authOpen = activeSection === "auth";
  const secretsOpen = activeSection === "secrets";
  const domainsOpen = activeSection === "domains";
  const mailboxOpen = activeSection === "mailbox";
  const skillsOpen = activeSection === "skills";
  const proofsOpen = activeSection === "proofs";
  const runsOpen = activeSection === "runs";
  const runtimeOpen = activeSection === "runtime";
  const insightsOpen = activeSection === "insights";
  const evidenceOpen = activeSection === "evidence";
  const deployOpen = activeSection === "deployment";
  const showOverview = activeSection === "overview";
  const showDeploymentBanner =
    activeSection !== "deployment" &&
    (deployActive || deployment?.status === "failed");
  const overviewSections: AgentOverviewSectionItem[] = [
    {
      section: "skills",
      label: "Tools",
      value: String(skills.length),
      detail: `${tools.length} runtime tools`,
    },
    {
      section: "proofs",
      label: "Proofs",
      value: latestProof?.badge || "none",
      detail: `${proofs.length} proof runs`,
    },
    {
      section: "runs",
      label: "Runs",
      value: String(runs.length),
      detail: `${runSummary.failures} failures`,
    },
    {
      section: "insights",
      label: "Insights",
      value: "usage",
      detail: "calls, latency, and files",
    },
    {
      section: "runtime",
      label: "Runtime",
      value: upgrade?.current_version ? `v${upgrade.current_version}` : "unknown",
      detail: codeEditorEnabled ? "editor enabled" : "editor disabled",
    },
    {
      section: "access",
      label: "API access",
      value: agent.public ? "public" : "private",
      detail: "tokens and integration links",
    },
    {
      section: "auth",
      label: "Auth",
      value: "credentials",
      detail: "imported API auth",
    },
    {
      section: "secrets",
      label: "Secrets",
      value: "vault",
      detail: "agent runtime secrets",
    },
    {
      section: "domains",
      label: "Domains",
      value: "routing",
      detail: "custom frontend domains",
    },
    {
      section: "mailbox",
      label: "Email inbox",
      value: "email",
      detail: "inbound email to threads",
    },
    {
      section: "evidence",
      label: "Evidence",
      value: "dossier",
      detail: "trust, review, and simulations",
    },
    {
      section: "deployment",
      label: "Deployment",
      value: deployment ? deployment.status : "none",
      detail: "build and rollout timeline",
    },
  ];

  async function bumpRuntime() {
    if (!canQueueRuntimeUpgrade) return;
    setUpgradeBusy(true);
    setUpgradeErr(null);
    try {
      const next = await upgradeAgentRuntime(agent.name);
      onAgentUpdated(next);
      onSectionChange("deployment");
    } catch (ex) {
      setUpgradeErr(ex instanceof Error ? ex.message : String(ex));
    } finally {
      setUpgradeBusy(false);
    }
  }

  async function removeThisAgent() {
    if (deleteConfirm !== agent.name || deleteBusy) return;
    setDeleteBusy(true);
    setDeleteErr(null);
    try {
      await deleteAgent(agent.name);
      onAgentDeleted(agent.name);
    } catch (ex) {
      setDeleteErr(ex instanceof Error ? ex.message : String(ex));
      setDeleteBusy(false);
    }
  }

  async function toggleCodeEditor() {
    if (codeEditorBusy) return;
    setCodeEditorBusy(true);
    setCodeEditorErr(null);
    try {
      const next = codeEditorEnabled
        ? await disableAgentCodeEditor(agent.name)
        : await enableAgentCodeEditor(agent.name);
      onAgentUpdated(next);
    } catch (ex) {
      setCodeEditorErr(ex instanceof Error ? ex.message : String(ex));
    } finally {
      setCodeEditorBusy(false);
    }
  }

  async function refreshOpenApiAgent() {
    if (!openApiSourceUrl || agentDeployActive || openApiRefreshBusy) return;
    const confirmed = window.confirm(
      `Refresh ${agent.name}? This will regenerate and redeploy it from ${openApiSourceUrls.join(", ")}.`,
    );
    if (!confirmed) return;
    setOpenApiRefreshBusy(true);
    setOpenApiRefreshErr(null);
    try {
      await generateOpenApiAgent({
        url: openApiSourceUrl,
        urls: openApiSourceUrls.length > 1 ? openApiSourceUrls : undefined,
        name: agent.name,
        public: agent.public,
        refresh_existing: true,
      });
      onSectionChange("deployment");
      await onAgentRefreshed();
    } catch (ex) {
      setOpenApiRefreshErr(ex instanceof Error ? ex.message : String(ex));
    } finally {
      setOpenApiRefreshBusy(false);
    }
  }

  async function refreshLiveCard() {
    if (cardRefreshBusy || agentDeployActive) return;
    setCardRefreshBusy(true);
    setCardRefreshErr(null);
    try {
      // Intentionally wakes the agent to re-read its live card; the server
      // persists it so the follow-up list reflects it without another wake.
      await refreshAgentCard(agent.name);
      await onAgentRefreshed();
    } catch (ex) {
      setCardRefreshErr(ex instanceof Error ? ex.message : String(ex));
    } finally {
      setCardRefreshBusy(false);
    }
  }

  async function requestTemplateUpdate() {
    if (!templateLineage || templateUpdateBusy || agentDeployActive) return;
    setTemplateUpdateBusy(true);
    setTemplateUpdateErr(null);
    setTemplateUpdateResult(null);
    try {
      const out = await requestAgentTemplateUpdate(agent.name);
      setTemplateUpdateResult(out);
    } catch (ex) {
      setTemplateUpdateErr(ex instanceof Error ? ex.message : String(ex));
    } finally {
      setTemplateUpdateBusy(false);
    }
  }

  return (
    <SurfacePanel as="article" className="bg-runtime-bg p-4">
      <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-baseline gap-2">
            <h2 className="font-mono text-base font-semibold text-ink">
              {agent.name}
            </h2>
            <StateBadge
              status={agent.status}
              live={agent.status === "running" || agent.status === "ready"}
            />
            {deployment && <DeploymentPill status={deployment.status} />}
            <StateBadge
              status={latestProof?.badge || "unverified"}
              aria-label={`Proof status: ${latestProof?.badge || "unverified"}`}
              size="xs"
            />
            <span className="rounded-full border border-runtime-line-soft/60 px-2 py-0.5 text-[11px] text-ink-muted">
              {agent.public ? "public" : "private"}
            </span>
            <span className="text-[11px] text-ink-faint">
              v{agent.card?.version || agent.version}
            </span>
            {upgrade?.update_available && <RuntimePill />}
          </div>
          <p className="mt-2 max-w-3xl text-sm leading-relaxed text-ink-dim">
            {agent.card?.description || agent.description || "No description."}
          </p>
          <div className="mt-3 flex flex-wrap gap-2 text-xs">
            <ToolbarLink href={agent.repo_url} external size="xs">
              Gitea repo
            </ToolbarLink>
            {apiUrl && (
              <ToolbarLink href={apiUrl} external size="xs">
                Agent API
              </ToolbarLink>
            )}
            <ToolbarLink href={appApi.openapi} external size="xs">
              OpenAPI
            </ToolbarLink>
            {frontendUrl && (
              <ToolbarLink href={frontendUrl} external size="xs">
                Frontend
              </ToolbarLink>
            )}
          </div>
        </div>

        <div className="grid min-w-[220px] grid-cols-2 gap-2 text-xs">
          <SummaryMetric label="tools" value={skills.length} />
          <SummaryMetric label="tools" value={tools.length} />
          <SummaryMetric label="created" value={fmtDate(agent.created_at)} />
          <SummaryMetric label="runs" value={runs.length} />
          <SummaryMetric label="failures" value={runSummary.failures} />
          <SummaryMetric
            label="deploy"
            value={deployment ? deployment.status : "none"}
          />
          <SummaryMetric
            label="proof"
            value={latestProof ? latestProof.badge : "none"}
          />
          <SummaryMetric
            label="a2a-pack"
            value={
              upgrade?.current_version
                ? `v${upgrade.current_version}`
                : "unknown"
            }
          />
          <SummaryMetric
            label="template"
            value={templateLineageMiniValue(templateLineage)}
          />
          <SummaryMetric
            label="editor"
            value={codeEditorEnabled ? "on" : "off"}
          />
        </div>
      </div>

      {showDeploymentBanner && deployment && (
        <InlineAlert
          tone={deployment.status === "failed" ? "red" : "amber"}
          className="mt-4"
        >
          <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
            <div className="min-w-0">
              <div className="text-xs font-semibold">
                Deployment {deployment.status}
              </div>
              <div className="mt-1 text-xs leading-relaxed opacity-80">
                The deployment timeline is pinned because this agent needs runtime attention.
              </div>
            </div>
            <ToolbarButton
              type="button"
              onClick={() => onSectionChange("deployment")}
              size="sm"
              className="shrink-0"
            >
              Open deployment
            </ToolbarButton>
          </div>
        </InlineAlert>
      )}

      {runtimeOpen && showRuntimeUpgradePanel && (
        <InlineAlert tone="amber" className="mt-4">
          <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
            <div className="min-w-0">
              <div className="text-xs font-semibold">
                {runtimeUpgradeActive
                  ? "a2a-pack runtime redeploy in progress"
                  : "a2a-pack runtime update available"}
              </div>
              <div className="mt-1 text-xs leading-relaxed opacity-80">
                {runtimeUpgradeMessage(upgrade, deployment, agentDeployActive)}
              </div>
            </div>
            <ToolbarButton
              onClick={bumpRuntime}
              disabled={!canQueueRuntimeUpgrade}
              size="sm"
              className="shrink-0 border-signal-authority/45 bg-signal-authority/12 text-signal-authority hover:border-signal-authority/45 hover:bg-signal-authority/12"
            >
              {runtimeUpgradeButtonLabel({
                busy: upgradeBusy,
                active: runtimeUpgradeActive,
                blocked: agentDeployActive,
                canRedeploy: Boolean(upgrade?.can_redeploy),
              })}
            </ToolbarButton>
          </div>
          {upgradeErr && (
            <InlineAlert tone="red" role="alert" className="mt-2 text-xs">
              {upgradeErr}
            </InlineAlert>
          )}
        </InlineAlert>
      )}

      {secretsOpen && (
        <AgentSecretsManager
          agentName={agent.name}
          className="mt-4 border-t border-runtime-line-soft/60 pt-4"
        />
      )}

      {authOpen && <AgentAuthPanel agent={agent} />}

      {apiAccessOpen && <AgentApiAccessPanel agent={agent} urls={appApi} />}

      {domainsOpen && (
        <AgentDomainsPanel
          agent={agent}
          selectedHostname={selectedDomainHostname}
          search={location.search}
        />
      )}

      {mailboxOpen && <AgentMailboxPanel agent={agent} />}

      {insightsOpen && (
        <AgentInsightsPanel
          agent={agent}
          runs={runs}
          proofs={proofs}
          selectedCallId={selectedCallId}
          search={location.search}
        />
      )}

      {evidenceOpen && (
        <AgentEvidencePanel
          agent={agent}
          activeView={evidenceView}
          search={location.search}
        />
      )}

      {showOverview && (
        <AgentOverviewSections
          sections={overviewSections}
          onSectionChange={onSectionChange}
        />
      )}

      {skillsOpen && <AgentSkillsPanel skills={skills} />}

      {proofsOpen && (
        <div className="mt-4 border-t border-runtime-line-soft/60 pt-4">
          <AgentProofWorkbench
            agent={agent}
            proofs={proofs}
            selectedProofId={selectedProofId}
            search={location.search}
            onProofCreated={onProofCreated}
          />
        </div>
      )}

      {runsOpen && (
        <AgentRunsPanel
          agentName={agent.name}
          runs={runs}
          selectedRunId={selectedRunId}
          selectedRunView={selectedRunView}
          search={location.search}
        />
      )}

      {runtimeOpen && (
        <AgentRuntimePanel
          agent={agent}
          runtime={runtime}
          upgrade={upgrade}
          templateLineage={templateLineage}
          codeEditorEnabled={codeEditorEnabled}
          cardRefreshBusy={cardRefreshBusy}
          openApiRefreshBusy={openApiRefreshBusy}
          codeEditorBusy={codeEditorBusy}
          agentDeployActive={agentDeployActive}
          openApiSourceUrl={openApiSourceUrl}
          openApiRefreshErr={openApiRefreshErr}
          cardRefreshErr={cardRefreshErr}
          codeEditorErr={codeEditorErr}
          deleteOpen={deleteOpen}
          deleteConfirm={deleteConfirm}
          deleteBusy={deleteBusy}
          deleteErr={deleteErr}
          templateUpdateBusy={templateUpdateBusy}
          templateUpdateResult={templateUpdateResult}
          templateUpdateErr={templateUpdateErr}
          onRefreshCard={refreshLiveCard}
          onToggleCodeEditor={toggleCodeEditor}
          onRefreshOpenApi={refreshOpenApiAgent}
          onDeleteToggle={() => {
            setDeleteOpen((open) => !open);
            setDeleteErr(null);
            setDeleteConfirm("");
          }}
          onDeleteConfirmChange={setDeleteConfirm}
          onDelete={removeThisAgent}
          onRequestTemplateUpdate={requestTemplateUpdate}
        />
      )}


      {deployOpen && (
        <div className="mt-4 border-t border-runtime-line-soft/60 pt-4">
          <AgentDeploymentTimeline
            agent={agent}
            deployment={deployment}
          />
        </div>
      )}
    </SurfacePanel>
  );
}

type AgentOverviewSectionItem = {
  section: AgentDetailSection;
  label: string;
  value: string;
  detail: string;
};

function AgentOverviewSections({
  sections,
  onSectionChange,
}: {
  sections: AgentOverviewSectionItem[];
  onSectionChange: (section: AgentDetailSection) => void;
}) {
  return (
    <div className="mt-4 border-t border-runtime-line-soft/60 pt-4">
      <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-3">
        {sections.map((item) => (
          <button
            key={item.section}
            type="button"
            onClick={() => onSectionChange(item.section)}
            className="group rounded-lg border border-runtime-line-soft/60 bg-runtime-panel/40 p-3 text-left transition hover:border-runtime-line-strong hover:bg-runtime-panel/70"
          >
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0">
                <div className="truncate text-sm font-medium text-ink">
                  {item.label}
                </div>
                <div className="mt-1 truncate text-xs text-ink-muted">
                  {item.detail}
                </div>
              </div>
              <div className="shrink-0 text-right font-mono text-xs text-ink-dim">
                {item.value}
              </div>
            </div>
          </button>
        ))}
      </div>
    </div>
  );
}

function AgentSkillsPanel({
  skills,
}: {
  skills: MyAgentListing["card"]["skills"];
}) {
  return (
    <div className="mt-4 border-t border-runtime-line-soft/60 pt-4">
      <div className="text-[10px] uppercase text-ink-faint">
        Skill catalog
      </div>
      {skills.length === 0 ? (
        <SurfacePanel as="div" className="mt-2 bg-runtime-panel/40 p-3 text-xs text-ink-muted">
          No live skill card yet. First build may still be registering.
        </SurfacePanel>
      ) : (
        <div className="mt-2 grid gap-2 md:grid-cols-2">
          {skills.map((skill) => (
            <SurfacePanel
              as="article"
              key={skill.name}
              className="bg-runtime-panel/40 p-3"
            >
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <div className="truncate font-mono text-xs text-ink">
                    {skill.name}
                  </div>
                  {skill.description && (
                    <p className="mt-1 line-clamp-3 text-[11px] leading-relaxed text-ink-muted">
                      {skill.description}
                    </p>
                  )}
                </div>
                {skill.tags.length > 0 && (
                  <span className="shrink-0 rounded-full border border-runtime-line-soft/60 px-2 py-0.5 text-[11px] text-ink-muted">
                    {skill.tags.length} tags
                  </span>
                )}
              </div>
              <div className="mt-3 grid gap-2 text-[11px] text-ink-muted sm:grid-cols-2">
                <DefinitionRow
                  label="input fields"
                  value={String(Object.keys(skill.input_schema || {}).length)}
                />
                <DefinitionRow
                  label="output fields"
                  value={String(Object.keys(skill.output_schema || {}).length)}
                />
              </div>
            </SurfacePanel>
          ))}
        </div>
      )}
    </div>
  );
}
