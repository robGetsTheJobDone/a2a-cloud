import { useEffect, useState, type FormEvent } from "react";
import {
  updateComplianceAgent,
  type ComplianceAgentClassification,
  type ComplianceRiskTier,
} from "../../api";
import {
  EmptyState,
  FormField,
  InlineAlert,
  LoadingState,
  SectionPanel,
  SelectInput,
  SurfacePanel,
  TextArea,
  TextInput,
  ToolbarButton,
} from "../DashboardChrome";
import { StatusBadge } from "../StatusPillAdapters";
import {
  COMPLIANCE_RISK_TIERS,
  formatComplianceDate,
  riskTierLabel,
  riskTierTone,
} from "./shared";

/**
 * AgentRiskPanel — per-agent EU AI Act risk classification. Each agent row is
 * an editable card: risk tier, intended purpose, human oversight, and notes,
 * saved per row.
 */
export function AgentRiskPanel({
  orgSlug,
  agents,
  loading,
  onSaved,
}: {
  orgSlug: string;
  agents: ComplianceAgentClassification[];
  loading: boolean;
  onSaved: () => Promise<void>;
}) {
  return (
    <SectionPanel
      title="Agent risk classification"
      description="Classify every agent under the EU AI Act risk tiers and document its intended purpose and human oversight."
    >
      {loading && agents.length === 0 ? (
        <LoadingState label="Loading agents..." />
      ) : agents.length === 0 ? (
        <EmptyState
          title="No agents to classify"
          description="Agents owned by this organization will appear here for risk-tier classification."
        />
      ) : (
        <div className="grid gap-3">
          {agents.map((agent) => (
            <AgentRiskRow
              key={agent.agent_name}
              orgSlug={orgSlug}
              agent={agent}
              onSaved={onSaved}
            />
          ))}
        </div>
      )}
    </SectionPanel>
  );
}

type AgentDraft = {
  riskTier: ComplianceRiskTier;
  intendedPurpose: string;
  humanOversight: string;
  notes: string;
};

function draftFromAgent(agent: ComplianceAgentClassification): AgentDraft {
  return {
    riskTier: agent.risk_tier,
    intendedPurpose: agent.intended_purpose ?? "",
    humanOversight: agent.human_oversight ?? "",
    notes: agent.notes ?? "",
  };
}

function isDirty(draft: AgentDraft, agent: ComplianceAgentClassification) {
  return (
    draft.riskTier !== agent.risk_tier ||
    draft.intendedPurpose !== (agent.intended_purpose ?? "") ||
    draft.humanOversight !== (agent.human_oversight ?? "") ||
    draft.notes !== (agent.notes ?? "")
  );
}

function AgentRiskRow({
  orgSlug,
  agent,
  onSaved,
}: {
  orgSlug: string;
  agent: ComplianceAgentClassification;
  onSaved: () => Promise<void>;
}) {
  const [draft, setDraft] = useState<AgentDraft>(() => draftFromAgent(agent));
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  // Re-seed when the server copy changes (e.g. after refresh or org switch).
  useEffect(() => {
    setDraft(draftFromAgent(agent));
    setErr(null);
  }, [agent]);

  const dirty = isDirty(draft, agent);

  async function handleSave(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setErr(null);
    setSaved(false);
    try {
      await updateComplianceAgent(orgSlug, agent.agent_name, {
        risk_tier: draft.riskTier,
        intended_purpose: draft.intendedPurpose.trim(),
        human_oversight: draft.humanOversight.trim(),
        notes: draft.notes.trim(),
      });
      await onSaved();
      setSaved(true);
    } catch (ex) {
      setErr(ex instanceof Error ? ex.message : String(ex));
    } finally {
      setBusy(false);
    }
  }

  return (
    <SurfacePanel as="article" className="bg-runtime-bg/70 p-4">
      <form onSubmit={handleSave} className="grid gap-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <span className="truncate font-mono text-sm text-ink">
                {agent.agent_name}
              </span>
              {agent.agent_version && (
                <span className="font-mono text-xs text-ink-faint">
                  v{agent.agent_version}
                </span>
              )}
              <StatusBadge tone={riskTierTone(agent.risk_tier)}>
                {riskTierLabel(agent.risk_tier)}
              </StatusBadge>
            </div>
            <div className="mt-1 text-xs text-ink-muted">
              Last classified {formatComplianceDate(agent.updated_at)}
            </div>
          </div>
          <ToolbarButton
            type="submit"
            variant="primary"
            size="sm"
            disabled={busy || !dirty}
          >
            {busy ? "Saving..." : "Save"}
          </ToolbarButton>
        </div>

        {err && <InlineAlert tone="red">{err}</InlineAlert>}
        {saved && !dirty && !err && (
          <InlineAlert tone="emerald">Classification saved.</InlineAlert>
        )}

        <div className="grid gap-3 lg:grid-cols-[180px_1fr_1fr]">
          <FormField label="Risk tier">
            <SelectInput
              compact
              value={draft.riskTier}
              onChange={(event) =>
                setDraft((current) => ({
                  ...current,
                  riskTier: event.target.value as ComplianceRiskTier,
                }))
              }
            >
              {COMPLIANCE_RISK_TIERS.map((tier) => (
                <option key={tier.id} value={tier.id}>
                  {tier.label}
                </option>
              ))}
            </SelectInput>
          </FormField>
          <FormField label="Intended purpose">
            <TextInput
              compact
              value={draft.intendedPurpose}
              placeholder="What this agent is meant to do"
              onChange={(event) =>
                setDraft((current) => ({
                  ...current,
                  intendedPurpose: event.target.value,
                }))
              }
            />
          </FormField>
          <FormField label="Human oversight">
            <TextInput
              compact
              value={draft.humanOversight}
              placeholder="How humans supervise or approve its decisions"
              onChange={(event) =>
                setDraft((current) => ({
                  ...current,
                  humanOversight: event.target.value,
                }))
              }
            />
          </FormField>
        </div>

        <FormField label="Notes">
          <TextArea
            rows={2}
            value={draft.notes}
            placeholder="Assessment notes, references, or mitigations"
            onChange={(event) =>
              setDraft((current) => ({ ...current, notes: event.target.value }))
            }
          />
        </FormField>
      </form>
    </SurfacePanel>
  );
}
