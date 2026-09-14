import {
  EmptyState,
  SelectableSurfaceButton,
} from "../DashboardChrome";
import { StateBadge } from "../StatusPillAdapters";
import type { KernelEvolutionRun, ProtocolSimulation } from "../../api";
import { runResult, textValue } from "./simulationModel";

/**
 * SimulationEvolutionRunList — left-rail evolution run list. Selection mutates
 * hoisted state via onSelect (mandate B/C) instead of navigating to a route,
 * so the lab never unmounts and the detail opens as an in-place sheet.
 */
export function SimulationEvolutionRunList({
  evolutionRuns,
  selectedEvolutionRunId,
  onSelect,
}: {
  evolutionRuns: KernelEvolutionRun[];
  selectedEvolutionRunId: string;
  onSelect: (jobId: string) => void;
}) {
  return (
    <div className="space-y-2 p-3">
      <div className="text-xs font-semibold uppercase text-ink-muted">
        Evolution runs
      </div>
      {evolutionRuns.length === 0 ? (
        <EmptyState size="compact" title="No evolution runs yet" className="py-4" />
      ) : (
        evolutionRuns.map((run) => {
          const result = runResult(run);
          const winnerId = textValue(result?.winner_variant_id, "no winner");
          const id = String(run.job.job_id);
          return (
            <SelectableSurfaceButton
              key={run.job.job_id}
              onClick={() => onSelect(id)}
              selected={selectedEvolutionRunId === id}
              className="flex items-center justify-between gap-3 px-3 py-2"
            >
              <span className="min-w-0">
                <span className="block truncate text-xs font-medium text-ink-soft">
                  {String(run.job.title || run.job.job_id)}
                </span>
                <span className="mt-0.5 block truncate font-mono text-[11px] text-ink-faint">
                  {winnerId}
                </span>
              </span>
              <StateBadge status={run.job.status} size="xs" className="shrink-0" />
            </SelectableSurfaceButton>
          );
        })
      )}
    </div>
  );
}

/**
 * SimulationRunList — left-rail recent-run list. Selection is state-driven
 * (mandate B/C) so opening a run flips the detail sheet without a route change.
 */
export function SimulationRunList({
  runs,
  selectedRunId,
  onSelect,
}: {
  runs: ProtocolSimulation[];
  selectedRunId: string;
  onSelect: (jobId: string) => void;
}) {
  return (
    <div className="space-y-2 p-3">
      <div className="text-xs font-semibold uppercase text-ink-muted">
        Recent runs
      </div>
      {runs.length === 0 ? (
        <EmptyState size="compact" title="No runs yet" className="py-4" />
      ) : (
        runs.map((run) => {
          const id = String(run.job.job_id);
          return (
            <SelectableSurfaceButton
              key={run.job.job_id}
              onClick={() => onSelect(id)}
              selected={selectedRunId === id}
              className="flex items-center justify-between gap-3 px-3 py-2"
            >
              <span className="min-w-0">
                <span className="block truncate text-xs font-medium text-ink-soft">
                  {String(run.job.title || run.job.job_id)}
                </span>
                <span className="mt-0.5 block truncate font-mono text-[11px] text-ink-faint">
                  {run.job.job_id}
                </span>
              </span>
              <StateBadge status={run.job.status} size="xs" className="shrink-0" />
            </SelectableSurfaceButton>
          );
        })
      )}
    </div>
  );
}
