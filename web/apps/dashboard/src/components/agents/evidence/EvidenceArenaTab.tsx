import { useState } from "react";
import type { CustomKernelSuiteTemplate, ProtocolSimulation } from "../../../api";
import { findArenaScoreboardView, type ArenaScoreboardView } from "../../../arenaScoreboard";
import {
  FormField,
  InlineAlert,
  SelectInput,
  SurfacePanel,
  ToolbarButton,
} from "../../DashboardChrome";
import { StateBadge, StatusBadge } from "../../StatusPillAdapters";
import { ArenaScoreboardCard, KernelTemplateSummaryPanel } from "./evidenceShared";

export function EvidenceArenaTab({
  templates,
  simulation,
  historicalScoreboard,
  busy,
  error,
  onRun,
}: {
  templates: CustomKernelSuiteTemplate[];
  simulation: ProtocolSimulation | null;
  historicalScoreboard: ArenaScoreboardView | null;
  busy: boolean;
  error: string | null;
  onRun: (templateId: string) => void;
}) {
  const [selectedTemplateId, setSelectedTemplateId] = useState("");
  const selectedTemplate =
    templates.find((template) => template.template_id === selectedTemplateId) ||
    templates[0] ||
    null;
  const scoreboard = simulation
    ? findArenaScoreboardView([simulation, ...(simulation.events || [])])
    : historicalScoreboard;
  return (
    <SurfacePanel as="section" className="mt-3 bg-runtime-bg/70 p-3">
      <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-[10px] uppercase text-ink-faint">
              Arena suite scoreboard
            </span>
            {simulation && <StateBadge status={simulation.job.status} size="xs" />}
            {scoreboard && (
              <StatusBadge tone={scoreboard.passed ? "emerald" : "red"}>
                {scoreboard.passed
                  ? `${scoreboard.episodeCount} episodes`
                  : `${scoreboard.failedEpisodeCount} failed`}
              </StatusBadge>
            )}
          </div>
          <div className="mt-1 max-w-2xl text-xs leading-relaxed text-ink-muted">
            Multi-episode arena results with winner, exclusion, score, and evidence refs.
          </div>
          {simulation && (
            <div className="mt-2 truncate font-mono text-[11px] text-ink-faint">
              {simulation.job.job_id}
            </div>
          )}
        </div>
        <ToolbarButton
          type="button"
          onClick={() => selectedTemplate && onRun(selectedTemplate.template_id)}
          disabled={busy || !selectedTemplate}
          variant="primary"
          size="sm"
          className="shrink-0"
        >
          {busy ? "running..." : "run suite"}
        </ToolbarButton>
      </div>

      <div className="mt-3 grid gap-2 lg:grid-cols-[minmax(0,260px)_minmax(0,1fr)]">
        <FormField label="Template">
          <SelectInput
            value={selectedTemplate?.template_id || ""}
            onChange={(event) => setSelectedTemplateId(event.target.value)}
            compact
          >
            {templates.length === 0 && <option value="">no suite templates</option>}
            {templates.map((template) => (
              <option key={template.template_id} value={template.template_id}>
                {template.name}
              </option>
            ))}
          </SelectInput>
        </FormField>
        <KernelTemplateSummaryPanel
          template={selectedTemplate}
          emptyDescription="No arena suite templates are available yet."
        />
      </div>

      {error && (
        <InlineAlert tone="red" role="alert" className="mt-2 text-xs">
          {error}
        </InlineAlert>
      )}

      {scoreboard && (
        <ArenaScoreboardCard
          scoreboard={scoreboard}
          compact={scoreboard.participants.length === 0}
        />
      )}
    </SurfacePanel>
  );
}
