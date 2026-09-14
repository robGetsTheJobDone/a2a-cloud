import type { CustomKernelSimulationTemplate, ProtocolSimulation } from "../../../api";
import { parseKernelTraceView } from "../../../kernelTrace";
import { KernelTraceCard } from "../../KernelTraceCard";
import {
  FormField,
  InlineAlert,
  SelectInput,
  SurfacePanel,
  TextArea,
  ToolbarButton,
} from "../../DashboardChrome";
import { StateBadge, StatusBadge } from "../../StatusPillAdapters";
import { KernelTemplateSummaryPanel, numberValue, objectValue } from "./evidenceShared";

export function EvidenceProtocolTab({
  spec,
  templates,
  selectedTemplateId,
  simulation,
  busy,
  error,
  onTemplateSelect,
  onSpecChange,
  onRun,
}: {
  spec: string;
  templates: CustomKernelSimulationTemplate[];
  selectedTemplateId: string;
  simulation: ProtocolSimulation | null;
  busy: boolean;
  error: string | null;
  onTemplateSelect: (templateId: string) => void;
  onSpecChange: (value: string) => void;
  onRun: () => void;
}) {
  const trace = simulation ? parseKernelTraceView(simulation, { traceLimit: 4 }) : null;
  const selectedTemplate =
    templates.find((template) => template.template_id === selectedTemplateId) || null;
  const latestTraceEvent =
    simulation?.events
      ?.slice()
      .reverse()
      .find((event) => event.event_type === "scenario_trace_recorded") || null;
  const traceSummary = objectValue(latestTraceEvent?.payload?.trace_summary);
  const invariantFailures = numberValue(traceSummary.invariant_fail_count);
  return (
    <SurfacePanel as="section" className="mt-3 bg-runtime-bg/70 p-3">
      <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-[10px] uppercase text-ink-faint">
              Custom kernel simulation
            </span>
            {simulation && <StateBadge status={simulation.job.status} size="xs" />}
            {trace && (
              <StatusBadge tone={trace.passed ? "emerald" : "red"}>
                {trace.passed ? "passed" : `${invariantFailures || trace.invariantFailCount} failed`}
              </StatusBadge>
            )}
          </div>
          {simulation && (
            <div className="mt-2 truncate font-mono text-[11px] text-ink-faint">
              {simulation.job.job_id}
            </div>
          )}
        </div>
        <ToolbarButton
          type="button"
          onClick={onRun}
          disabled={busy}
          variant="primary"
          size="sm"
          className="shrink-0"
        >
          {busy ? "running..." : "run simulation"}
        </ToolbarButton>
      </div>

      <div className="mt-3 grid gap-2 lg:grid-cols-[minmax(0,260px)_minmax(0,1fr)]">
        <FormField label="Template">
          <SelectInput
            value={selectedTemplateId}
            onChange={(event) => onTemplateSelect(event.target.value)}
            compact
          >
            <option value="">raw spec</option>
            {templates.map((template) => (
              <option key={template.template_id} value={template.template_id}>
                {template.name}
              </option>
            ))}
          </SelectInput>
        </FormField>
        <KernelTemplateSummaryPanel
          template={selectedTemplate}
          emptyDescription="Raw specs run without template provenance. Choose a template to bind the run to a reusable kernel shape."
        />
      </div>

      <TextArea
        value={spec}
        onChange={(event) => onSpecChange(event.target.value)}
        rows={12}
        spellCheck={false}
        aria-label="Custom kernel simulation spec"
        mono
        compact
        className="mt-3 leading-relaxed"
      />

      {error && (
        <InlineAlert tone="red" role="alert" className="mt-2 text-xs">
          {error}
        </InlineAlert>
      )}

      {trace && <KernelTraceCard trace={trace} compact />}
    </SurfacePanel>
  );
}
