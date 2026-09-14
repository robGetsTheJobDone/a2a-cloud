import { useCallback, useEffect, useMemo, useState } from "react";
import {
  createReviewLoop,
  getAgentDossier,
  getAgentEvidenceTimeline,
  listReviewLoops,
  listCustomKernelSimulationTemplates,
  listCustomKernelSuiteTemplates,
  stopReviewLoop,
  runCustomProtocolSimulation,
  runCustomProtocolSuite,
  type AgentDossier,
  type EvidenceTimeline,
  type CustomKernelSimulationTemplate,
  type CustomKernelSuiteTemplate,
  type MyAgentListing,
  type ProtocolSimulation,
  type ReviewLoop,
} from "../../api";
import { findArenaScoreboardView } from "../../arenaScoreboard";
import {
  InlineAlert,
  SegmentedControl,
  TabPill,
  ToolbarButton,
} from "../DashboardChrome";
import { type AgentEvidenceView } from "./agentTypes";
import {
  DEFAULT_CUSTOM_KERNEL_SIMULATION_SPEC_TEXT,
  EvidenceFlag,
  booleanValue,
  numberValue,
  shortRef,
  textValue,
} from "./evidence/evidenceShared";
import { EvidenceDossierTab } from "./evidence/EvidenceDossierTab";
import { EvidenceTimelineTab } from "./evidence/EvidenceTimelineTab";
import { EvidenceReviewTab } from "./evidence/EvidenceReviewTab";
import { EvidenceProtocolTab } from "./evidence/EvidenceProtocolTab";
import { EvidenceArenaTab } from "./evidence/EvidenceArenaTab";

// Internal sub-tab ids rendered inside the agent DetailSheet body. The legacy
// route-driven evidence view ("overview" | "timeline" | "review" |
// "simulations") is mapped onto these on mount so existing deep-links keep
// hydrating, but tab switching is local state — no route reload (mandate B).
type EvidenceTabId = "dossier" | "timeline" | "review" | "protocol" | "arena";

const EVIDENCE_TABS: Array<{ id: EvidenceTabId; label: string; description: string }> = [
  {
    id: "dossier",
    label: "Dossier",
    description: "Trust posture, evidence metrics, and dossier warnings.",
  },
  {
    id: "timeline",
    label: "Timeline",
    description: "Evidence lanes, provenance rows, inferred signals, and audit chronology.",
  },
  {
    id: "review",
    label: "Review loops",
    description: "Adversarial reviewer loops, proposed fixes, freezes, and reviewer events.",
  },
  {
    id: "protocol",
    label: "Protocol",
    description: "Custom kernel protocol simulations and replay traces.",
  },
  {
    id: "arena",
    label: "Arena",
    description: "Arena suite scoreboards across multi-episode runs.",
  },
];

function tabFromEvidenceView(view: AgentEvidenceView): EvidenceTabId {
  if (view === "overview") return "dossier";
  if (view === "simulations") return "protocol";
  return view;
}

export function AgentEvidencePanel({
  agent,
  activeView,
}: {
  agent: MyAgentListing;
  activeView: AgentEvidenceView;
  /** Retained for compatibility with the route-driven caller; unused now. */
  search?: string;
}) {
  const [dossier, setDossier] = useState<AgentDossier | null>(null);
  const [timeline, setTimeline] = useState<EvidenceTimeline | null>(null);
  const [reviewLoops, setReviewLoops] = useState<ReviewLoop[]>([]);
  const [customTemplates, setCustomTemplates] = useState<CustomKernelSimulationTemplate[]>([]);
  const [suiteTemplates, setSuiteTemplates] = useState<CustomKernelSuiteTemplate[]>([]);
  const [tab, setTab] = useState<EvidenceTabId>(() => tabFromEvidenceView(activeView));
  const [busy, setBusy] = useState(false);
  const [loopBusy, setLoopBusy] = useState(false);
  const [customSimulationBusy, setCustomSimulationBusy] = useState(false);
  const [suiteBusy, setSuiteBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [loopErr, setLoopErr] = useState<string | null>(null);
  const [customSimulationErr, setCustomSimulationErr] = useState<string | null>(null);
  const [suiteErr, setSuiteErr] = useState<string | null>(null);
  const [customSimulation, setCustomSimulation] = useState<ProtocolSimulation | null>(null);
  const [suiteSimulation, setSuiteSimulation] = useState<ProtocolSimulation | null>(null);
  const [customSimulationSpec, setCustomSimulationSpec] = useState(
    DEFAULT_CUSTOM_KERNEL_SIMULATION_SPEC_TEXT,
  );
  const [selectedCustomTemplateId, setSelectedCustomTemplateId] = useState("");

  // Deep-link hydration: a fresh route view (e.g. another agent opened with a
  // /evidence/review URL) re-aligns the active tab without remounting.
  useEffect(() => {
    setTab(tabFromEvidenceView(activeView));
  }, [activeView]);

  const load = useCallback(async () => {
    setBusy(true);
    setErr(null);
    try {
      const [
        nextDossier,
        nextTimeline,
        nextReviewLoops,
        nextCustomTemplates,
        nextSuiteTemplates,
      ] = await Promise.all([
        getAgentDossier(agent.name),
        getAgentEvidenceTimeline(agent.name, {
          limit: 120,
          include_inferred: true,
        }),
        listReviewLoops(agent.name, 8),
        listCustomKernelSimulationTemplates(agent.name),
        listCustomKernelSuiteTemplates(agent.name),
      ]);
      setDossier(nextDossier);
      setTimeline(nextTimeline);
      setReviewLoops(nextReviewLoops);
      setCustomTemplates(nextCustomTemplates);
      setSuiteTemplates(nextSuiteTemplates);
    } catch (ex) {
      setErr(ex instanceof Error ? ex.message : String(ex));
    } finally {
      setBusy(false);
    }
  }, [agent.name]);

  useEffect(() => {
    load();
  }, [load]);

  const historicalArenaScoreboard = useMemo(
    () => findArenaScoreboardView(timeline?.items || []),
    [timeline],
  );

  const currentVersion = dossier?.current_version || {};
  const trustProfile = dossier?.trust_profile || {};
  const authority = dossier?.authority_summary || {};
  const quality = dossier?.quality_summary || {};
  const mutation = dossier?.mutation_summary || {};
  const risk = dossier?.risk_summary || {};
  const headSha = textValue(currentVersion.head_sha) || textValue(currentVersion.proof_id) || "-";
  const warningCount = numberValue(risk.warning_count);
  const strictTrust = booleanValue(trustProfile.strict_version_trust);
  const strictAuthority = booleanValue(trustProfile.strict_authority_execution);
  const strictRemediation = booleanValue(trustProfile.strict_failure_remediation);

  async function startReviewLoop() {
    if (loopBusy) return;
    setLoopBusy(true);
    setLoopErr(null);
    try {
      await createReviewLoop(agent.name, {
        ref: "main",
        loop_budget_cents: 300,
        budget_ceiling_cents: 500,
        ttl_seconds: 900,
        max_iterations: 3,
      });
      await load();
    } catch (ex) {
      setLoopErr(ex instanceof Error ? ex.message : String(ex));
    } finally {
      setLoopBusy(false);
    }
  }

  async function stopLoop(jobId: string) {
    if (loopBusy) return;
    setLoopBusy(true);
    setLoopErr(null);
    try {
      await stopReviewLoop(agent.name, jobId, "owner stopped reviewer loop from dossier");
      await load();
    } catch (ex) {
      setLoopErr(ex instanceof Error ? ex.message : String(ex));
    } finally {
      setLoopBusy(false);
    }
  }

  async function runCustomSimulation() {
    if (customSimulationBusy) return;
    setCustomSimulationBusy(true);
    setCustomSimulationErr(null);
    try {
      const parsed = JSON.parse(customSimulationSpec) as unknown;
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
        throw new Error("Simulation spec must be a JSON object.");
      }
      const request = selectedCustomTemplateId
        ? { template_id: selectedCustomTemplateId, cost_cents: 0 }
        : { spec: parsed as Record<string, unknown>, cost_cents: 0 };
      const next = await runCustomProtocolSimulation(agent.name, {
        ...request,
      });
      setCustomSimulation(next);
      await load();
    } catch (ex) {
      setCustomSimulationErr(ex instanceof Error ? ex.message : String(ex));
    } finally {
      setCustomSimulationBusy(false);
    }
  }

  async function runArenaSuite(templateId: string) {
    if (suiteBusy || !templateId) return;
    setSuiteBusy(true);
    setSuiteErr(null);
    try {
      const next = await runCustomProtocolSuite(agent.name, {
        template_id: templateId,
        cost_cents: 0,
      });
      setSuiteSimulation(next);
      await load();
    } catch (ex) {
      setSuiteErr(ex instanceof Error ? ex.message : String(ex));
    } finally {
      setSuiteBusy(false);
    }
  }

  const currentTab = EVIDENCE_TABS.find((view) => view.id === tab) || EVIDENCE_TABS[0];

  return (
    <div className="mt-4 border-t border-runtime-line-soft/60 pt-4">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <div className="text-[10px] uppercase text-ink-faint">Evidence dossier</div>
          <div className="mt-1 flex flex-wrap items-center gap-2 text-xs">
            <span className="font-mono text-ink-soft">{shortRef(headSha)}</span>
            <EvidenceFlag ok={strictTrust} label="version trust" />
            <EvidenceFlag ok={strictAuthority} label="authority" />
            <EvidenceFlag ok={strictRemediation} label="remediation" />
            {warningCount > 0 && (
              <span className="rounded-full border border-signal-authority/45 bg-signal-authority/12 px-2 py-0.5 text-signal-authority">
                {warningCount} warnings
              </span>
            )}
          </div>
        </div>
        <ToolbarButton onClick={load} disabled={busy} size="sm">
          {busy ? "Loading..." : "Refresh"}
        </ToolbarButton>
      </div>

      {err && (
        <InlineAlert tone="red" role="alert" className="mt-3 text-xs">
          {err}
        </InlineAlert>
      )}

      <div className="mt-4 space-y-2">
        <SegmentedControl
          role="tablist"
          aria-label="Evidence views"
          className="flex flex-wrap gap-1 bg-runtime-bg"
        >
          {EVIDENCE_TABS.map((view) => (
            <TabPill
              key={view.id}
              onClick={() => setTab(view.id)}
              selected={view.id === tab}
            >
              {view.label}
            </TabPill>
          ))}
        </SegmentedControl>
        <div className="text-xs leading-relaxed text-ink-muted">
          {currentTab.description}
        </div>
      </div>

      {tab === "dossier" && (
        <EvidenceDossierTab
          dossier={dossier}
          quality={quality}
          authority={authority}
          mutation={mutation}
          risk={risk}
        />
      )}

      {tab === "review" && (
        <EvidenceReviewTab
          loops={reviewLoops}
          busy={loopBusy}
          error={loopErr}
          quality={quality}
          mutation={mutation}
          risk={risk}
          onStart={startReviewLoop}
          onStop={stopLoop}
        />
      )}

      {tab === "protocol" && (
        <EvidenceProtocolTab
          spec={customSimulationSpec}
          templates={customTemplates}
          selectedTemplateId={selectedCustomTemplateId}
          simulation={customSimulation}
          busy={customSimulationBusy}
          error={customSimulationErr}
          onTemplateSelect={(templateId) => {
            setSelectedCustomTemplateId(templateId);
            const template = customTemplates.find((item) => item.template_id === templateId);
            if (template) {
              setCustomSimulationSpec(JSON.stringify(template.spec, null, 2));
            }
          }}
          onSpecChange={(next) => {
            setSelectedCustomTemplateId("");
            setCustomSimulationSpec(next);
          }}
          onRun={runCustomSimulation}
        />
      )}

      {tab === "arena" && (
        <EvidenceArenaTab
          templates={suiteTemplates}
          simulation={suiteSimulation}
          historicalScoreboard={historicalArenaScoreboard}
          busy={suiteBusy}
          error={suiteErr}
          onRun={runArenaSuite}
        />
      )}

      {tab === "timeline" && (
        <EvidenceTimelineTab timeline={timeline} hasError={Boolean(err)} />
      )}
    </div>
  );
}

