import {
  EmptyState,
  FormField,
  InlineAlert,
  LoadingState,
  SelectInput,
  SurfacePanel,
  SummaryMetric,
  SummaryStrip,
  TextArea,
  TextInput,
  ToolbarButton,
} from "../DashboardChrome";
import { StatusBadge } from "../StatusPillAdapters";
import { BUILDER_KINDS, defaultSkill } from "../../kernelSimulationBuilder";
import type { AgentListing, UserKernelSimulationTemplate } from "../../api";
import { simulationKindLabel, type SimulationSpecReadiness } from "./simulationModel";

export function SimulationBuilderPanel({
  builderKind,
  setBuilderKind,
  builderTitle,
  setBuilderTitle,
  builderGoal,
  setBuilderGoal,
  availableAgents,
  selectedAgentNames,
  toggleAgent,
  selectedAgentCount,
  resourceLoading,
  onBuild,
}: {
  builderKind: string;
  setBuilderKind: (value: string) => void;
  builderTitle: string;
  setBuilderTitle: (value: string) => void;
  builderGoal: string;
  setBuilderGoal: (value: string) => void;
  availableAgents: AgentListing[];
  selectedAgentNames: string[];
  toggleAgent: (name: string) => void;
  selectedAgentCount: number;
  resourceLoading: boolean;
  onBuild: () => void;
}) {
  return (
    <SurfacePanel as="div" className="bg-runtime-bg p-3">
      <div className="mb-3 flex items-center justify-between gap-3">
        <h2 className="text-sm font-semibold text-ink">Template builder</h2>
        <StatusBadge tone="neutral" className="rounded-md">
          agent-driven
        </StatusBadge>
      </div>
      <div className="grid gap-3">
        <FormField label="Shape">
          <SelectInput
            value={builderKind}
            onChange={(event) => setBuilderKind(event.target.value)}
          >
            {BUILDER_KINDS.map((kind) => (
              <option key={kind} value={kind}>
                {simulationKindLabel(kind)}
              </option>
            ))}
          </SelectInput>
        </FormField>
        <FormField label="Title">
          <TextInput
            value={builderTitle}
            onChange={(event) => setBuilderTitle(event.target.value)}
          />
        </FormField>
        <FormField label="Goal">
          <TextArea
            value={builderGoal}
            onChange={(event) => setBuilderGoal(event.target.value)}
            className="min-h-[84px]"
          />
        </FormField>
        <div>
          <div className="mb-2 text-xs font-medium text-ink-muted">Agents</div>
          <div className="max-h-[220px] space-y-2 overflow-auto pr-1">
            {availableAgents.length === 0 ? (
              resourceLoading ? (
                <LoadingState label="Loading agents..." />
              ) : (
                <EmptyState size="compact" title="No agents found" className="py-4" />
              )
            ) : (
              availableAgents.map((agent) => {
                const checked = selectedAgentNames.includes(agent.name);
                return (
                  <label
                    key={agent.name}
                    className={
                      "flex cursor-pointer items-start gap-2 rounded-md border px-3 py-2 " +
                      (checked
                        ? "border-signal-authority/45 bg-signal-authority/12"
                        : "border-runtime-line-soft/60 bg-runtime-panel/40 hover:border-runtime-line-strong")
                    }
                  >
                    <input
                      type="checkbox"
                      checked={checked}
                      onChange={() => toggleAgent(agent.name)}
                      className="mt-1 h-4 w-4 accent-signal-authority"
                    />
                    <span className="min-w-0">
                      <span className="block truncate text-sm font-medium text-ink">
                        {agent.name}
                      </span>
                      <span className="mt-0.5 block truncate text-xs text-ink-muted">
                        {defaultSkill(agent)} · {agent.status}
                      </span>
                    </span>
                  </label>
                );
              })
            )}
          </div>
        </div>
        <ToolbarButton
          type="button"
          onClick={onBuild}
          disabled={selectedAgentCount === 0}
          variant="primary"
          className="w-full"
        >
          Build spec
        </ToolbarButton>
      </div>
    </SurfacePanel>
  );
}

export function SimulationEvolutionLabPanel({
  evolutionTitle,
  setEvolutionTitle,
  evolutionSeed,
  setEvolutionSeed,
  evolutionVariantCount,
  setEvolutionVariantCount,
  evolutionMaxDelta,
  setEvolutionMaxDelta,
  evolutionParticipantsText,
  setEvolutionParticipantsText,
  evolutionBusy,
  onRunEvolution,
}: {
  evolutionTitle: string;
  setEvolutionTitle: (value: string) => void;
  evolutionSeed: string;
  setEvolutionSeed: (value: string) => void;
  evolutionVariantCount: number;
  setEvolutionVariantCount: (value: number) => void;
  evolutionMaxDelta: number;
  setEvolutionMaxDelta: (value: number) => void;
  evolutionParticipantsText: string;
  setEvolutionParticipantsText: (value: string) => void;
  evolutionBusy: boolean;
  onRunEvolution: () => void;
}) {
  return (
    <SurfacePanel as="div" className="bg-runtime-bg p-3">
      <div className="mb-3 flex items-center justify-between gap-3">
        <h2 className="text-sm font-semibold text-ink">Evolution lab</h2>
        <StatusBadge tone="amber">experimental</StatusBadge>
      </div>
      <div className="grid gap-3">
        <FormField label="Title">
          <TextInput
            value={evolutionTitle}
            onChange={(event) => setEvolutionTitle(event.target.value)}
          />
        </FormField>
        <div className="grid grid-cols-3 gap-2">
          <FormField label="Seed">
            <TextInput
              value={evolutionSeed}
              onChange={(event) => setEvolutionSeed(event.target.value)}
              compact
            />
          </FormField>
          <FormField label="Variants">
            <TextInput
              type="number"
              min={1}
              max={8}
              value={evolutionVariantCount}
              onChange={(event) => setEvolutionVariantCount(Number(event.target.value) || 1)}
              compact
            />
          </FormField>
          <FormField label="Delta">
            <TextInput
              type="number"
              min={0}
              max={25}
              value={evolutionMaxDelta}
              onChange={(event) => setEvolutionMaxDelta(Number(event.target.value) || 0)}
              compact
            />
          </FormField>
        </div>
        <FormField label="Participants">
          <TextArea
            value={evolutionParticipantsText}
            onChange={(event) => setEvolutionParticipantsText(event.target.value)}
            spellCheck={false}
            mono
            compact
            className="h-44 leading-4"
          />
        </FormField>
        <ToolbarButton
          type="button"
          onClick={onRunEvolution}
          disabled={evolutionBusy || !evolutionParticipantsText.trim()}
          variant="primary"
          className="w-full"
        >
          {evolutionBusy ? "evolving..." : "run evolution"}
        </ToolbarButton>
      </div>
    </SurfacePanel>
  );
}

export function SimulationTemplateListPanel({
  templates,
  selectedId,
  setSelectedId,
  resourceLoading,
  refreshing,
  onRefresh,
}: {
  templates: UserKernelSimulationTemplate[];
  selectedId: string;
  setSelectedId: (id: string) => void;
  resourceLoading: boolean;
  refreshing: boolean;
  onRefresh: () => void;
}) {
  return (
    <div className="p-3">
      <div className="mb-3 flex items-center justify-between gap-3">
        <h1 className="text-sm font-semibold text-ink">Templates</h1>
        <StatusBadge tone="emerald" className="rounded-md">
          simulation-only
        </StatusBadge>
      </div>
      <div className="grid gap-2">
        {templates.length === 0 ? (
          resourceLoading ? (
            <LoadingState label="Loading templates..." />
          ) : (
            <EmptyState
              size="compact"
              title="No templates found"
              description="Refresh the lab or build a live-agent spec from owned agents."
              action={
                <ToolbarButton
                  type="button"
                  onClick={onRefresh}
                  disabled={refreshing}
                >
                  {refreshing ? "refreshing..." : "Refresh"}
                </ToolbarButton>
              }
              className="py-4"
            />
          )
        ) : (
          templates.map((template) => (
            <button
              key={template.template_id}
              type="button"
              onClick={() => setSelectedId(template.template_id)}
              className={
                "min-h-[64px] rounded-md border px-3 py-2 text-left transition " +
                (template.template_id === selectedId
                  ? "border-signal-authority/45 bg-signal-authority/12"
                  : "border-runtime-line-soft/60 bg-runtime-panel/40 hover:border-runtime-line-strong")
              }
            >
              <span className="block truncate text-sm font-medium text-ink">
                {template.name}
              </span>
              <span className="mt-1 block text-xs capitalize text-ink-muted">
                {simulationKindLabel(template.simulation_type)}
              </span>
            </button>
          ))
        )}
      </div>
    </div>
  );
}

export function SimulationSpecEditorPanel({
  selected,
  executionMode,
  setExecutionMode,
  onRun,
  busy,
  specReadiness,
  activeRunElapsed,
  resourceLoading,
  refreshing,
  onRestoreTemplate,
  onOpenBuilder,
  specText,
  setSpecText,
}: {
  selected: UserKernelSimulationTemplate | null;
  executionMode: "bounded" | "hybrid";
  setExecutionMode: (mode: "bounded" | "hybrid") => void;
  onRun: () => void;
  busy: boolean;
  specReadiness: SimulationSpecReadiness;
  activeRunElapsed: string | null;
  resourceLoading: boolean;
  refreshing: boolean;
  onRestoreTemplate: () => void;
  onOpenBuilder: () => void;
  specText: string;
  setSpecText: (value: string) => void;
}) {
  return (
    <SurfacePanel as="div" className="bg-runtime-bg p-3">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <div>
          <h2 className="text-sm font-semibold text-ink">
            {selected?.name || "Simulation spec"}
          </h2>
          {selected && (
            <p className="mt-1 text-xs text-ink-muted">
              {simulationKindLabel(selected.simulation_type)}
            </p>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <FormField label="Mode" className="min-w-[132px]">
            <SelectInput
              value={executionMode}
              onChange={(event) => setExecutionMode(event.target.value as "bounded" | "hybrid")}
              compact
            >
              <option value="bounded">proof only</option>
              <option value="hybrid">live agents</option>
            </SelectInput>
          </FormField>
          <ToolbarButton
            type="button"
            onClick={onRun}
            disabled={busy || !specReadiness.canRun}
            variant="primary"
          >
            {busy ? "running..." : executionMode === "hybrid" ? "run live" : "run"}
          </ToolbarButton>
        </div>
      </div>
      {activeRunElapsed && (
        <InlineAlert tone="amber" role="status" className="mb-3 text-xs">
          Live request running for <span className="font-mono">{activeRunElapsed}</span>
          {executionMode === "hybrid" ? ". Agent results appear when the request completes." : "."}
        </InlineAlert>
      )}
      <SimulationSpecReadinessPanel
        readiness={specReadiness}
        loading={resourceLoading}
        refreshing={refreshing}
        hasSelectedTemplate={Boolean(selected)}
        onRestoreTemplate={onRestoreTemplate}
        onSwitchToProofOnly={() => setExecutionMode("bounded")}
        onOpenBuilder={onOpenBuilder}
      />
      <TextArea
        value={specText}
        onChange={(event) => setSpecText(event.target.value)}
        spellCheck={false}
        aria-label="Kernel simulation JSON spec"
        mono
        compact
        className="h-[56vh] min-h-[420px] leading-5"
      />
    </SurfacePanel>
  );
}

function SimulationSpecReadinessPanel({
  readiness,
  loading,
  refreshing,
  hasSelectedTemplate,
  onRestoreTemplate,
  onSwitchToProofOnly,
  onOpenBuilder,
}: {
  readiness: SimulationSpecReadiness;
  loading: boolean;
  refreshing: boolean;
  hasSelectedTemplate: boolean;
  onRestoreTemplate: () => void;
  onSwitchToProofOnly: () => void;
  onOpenBuilder: () => void;
}) {
  if (loading) {
    return (
      <div className="mb-3">
        <LoadingState label="Loading simulation inputs..." />
      </div>
    );
  }

  const sourceTone =
    readiness.liveSourceCount > 0
      ? "emerald"
      : readiness.state === "blocked"
        ? "amber"
        : "neutral";

  return (
    <SurfacePanel as="section" className="mb-3 bg-runtime-panel/50 px-3 py-2">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-xs font-semibold text-ink">run readiness</span>
            <StatusBadge tone={readiness.tone} dot={readiness.state === "ready"}>
              {readiness.label}
            </StatusBadge>
            {refreshing && (
              <StatusBadge tone="amber" dot>
                refreshing
              </StatusBadge>
            )}
          </div>
          <p className="mt-1 text-xs leading-relaxed text-ink-muted">
            {refreshing ? "Refreshing templates, runs, evolution results, and agents." : readiness.detail}
          </p>
        </div>
        {readiness.state === "empty" && (
          <ToolbarButton type="button" size="xs" onClick={onOpenBuilder}>
            Open builder
          </ToolbarButton>
        )}
        {readiness.state === "invalid" && hasSelectedTemplate && (
          <ToolbarButton type="button" size="xs" onClick={onRestoreTemplate}>
            Restore template
          </ToolbarButton>
        )}
        {readiness.state === "blocked" && (
          <div className="flex flex-wrap gap-2">
            <ToolbarButton type="button" size="xs" onClick={onSwitchToProofOnly}>
              Proof only
            </ToolbarButton>
            <ToolbarButton type="button" size="xs" onClick={onOpenBuilder}>
              Build live spec
            </ToolbarButton>
          </div>
        )}
      </div>

      <SummaryStrip className="mt-3 grid-cols-2 lg:grid-cols-4">
        <SummaryMetric
          label="actors"
          value={readiness.actorCount.toLocaleString()}
          detail="graph nodes"
          size="compact"
          tone={readiness.actorCount > 0 ? "emerald" : "neutral"}
        />
        <SummaryMetric
          label="steps"
          value={readiness.stepCount.toLocaleString()}
          detail="planned events"
          size="compact"
          tone={readiness.stepCount > 0 ? "emerald" : "neutral"}
        />
        <SummaryMetric
          label="invariants"
          value={readiness.invariantCount.toLocaleString()}
          detail="replay checks"
          size="compact"
          tone={readiness.invariantCount > 0 ? "emerald" : "neutral"}
        />
        <SummaryMetric
          label="live sources"
          value={readiness.liveSourceCount.toLocaleString()}
          detail="selected agents"
          size="compact"
          tone={sourceTone}
        />
      </SummaryStrip>
    </SurfacePanel>
  );
}
