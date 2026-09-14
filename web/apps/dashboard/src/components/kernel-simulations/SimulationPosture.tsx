import { SegmentedButton, ToolbarButton } from "../DashboardChrome";
import { DashboardSurfacePosture } from "../SurfacePosture";
import { SIMULATION_VIEWS, type SimulationViewId } from "../../navigation";
import type { UserKernelSimulationTemplate } from "../../api";
import {
  simulationNextAction,
  simulationPostureLabel,
  simulationPostureTone,
  type SimulationRunSummary,
} from "./simulationModel";

const POSTURE_TONE: Record<
  ReturnType<typeof simulationPostureTone>,
  "danger" | "authority" | "live" | "neutral"
> = {
  red: "danger",
  amber: "authority",
  emerald: "live",
  neutral: "neutral",
};

const POSTURE_PULSE: Record<ReturnType<typeof simulationPostureTone>, boolean> = {
  red: false,
  amber: true,
  emerald: false,
  neutral: false,
};

// Map a next-action href back to the in-app view it would have routed to, so
// the posture's primary CTA flips view state (mandate B/C) instead of issuing a
// full-page route redirect that unmounts the lab.
function viewForActionHref(href: string): SimulationViewId | null {
  if (href.startsWith("/simulations/runs")) return "runs";
  if (href.startsWith("/simulations/evolution-results")) return "evolution-results";
  if (href.startsWith("/simulations/evolution")) return "evolution";
  if (href.startsWith("/simulations/spec")) return "spec";
  if (href === "/simulations") return "builder";
  return null;
}

/**
 * SimulationLabPosture — single compact telemetry strip that fuses the surface
 * posture, the view selector, and the next-best action into one line (mandate
 * D). View tabs and the CTA mutate hoisted state via setActiveView — no route
 * redirects, so the lab never unmounts (mandate A/B/C).
 */
export function SimulationLabPosture({
  activeView,
  setActiveView,
  templates,
  selectedAgentCount,
  runSummary,
  evolutionSummary,
  selectedRunId,
  selectedEvolutionRunId,
  busy,
  evolutionBusy,
  activeRunElapsed,
}: {
  activeView: SimulationViewId;
  setActiveView: (view: SimulationViewId) => void;
  templates: UserKernelSimulationTemplate[];
  selectedAgentCount: number;
  runSummary: SimulationRunSummary;
  evolutionSummary: SimulationRunSummary;
  selectedRunId: string;
  selectedEvolutionRunId: string;
  busy: boolean;
  evolutionBusy: boolean;
  activeRunElapsed: string | null;
}) {
  const nextAction = simulationNextAction({
    activeView,
    templates,
    selectedAgentCount,
    runSummary,
    evolutionSummary,
    selectedRunId,
    selectedEvolutionRunId,
    busy,
    evolutionBusy,
    activeRunElapsed,
  });
  const tone = simulationPostureTone(runSummary, evolutionSummary, busy, evolutionBusy);
  const label = simulationPostureLabel(runSummary, evolutionSummary, busy, evolutionBusy);
  const nextActionView = viewForActionHref(nextAction.href);

  return (
    <div className="space-y-2" data-onboarding-target="simulation-lab-posture">
      <DashboardSurfacePosture
        eyebrow="simulation lab"
        title={nextAction.label}
        status={{ label, tone: POSTURE_TONE[tone], pulse: POSTURE_PULSE[tone] }}
        metrics={[
          {
            label: "templates",
            value: templates.length.toLocaleString(),
            tone: templates.length > 0 ? "live" : "neutral",
          },
          {
            label: "agents",
            value: selectedAgentCount.toLocaleString(),
            tone: selectedAgentCount > 0 ? "live" : "danger",
          },
          {
            label: "runs",
            value: `${runSummary.total.toLocaleString()} · ${runSummary.active.toLocaleString()} active`,
            tone: runSummary.failed > 0 ? "danger" : runSummary.active > 0 ? "authority" : "neutral",
          },
          {
            label: "evolution",
            value: `${evolutionSummary.total.toLocaleString()} · ${evolutionSummary.failed.toLocaleString()} failed`,
            tone: evolutionSummary.failed > 0 ? "danger" : evolutionSummary.done > 0 ? "live" : "neutral",
          },
          {
            label: "selected",
            value: selectedRunId || selectedEvolutionRunId || "none",
          },
        ]}
        actions={
          <>
            <div
              role="tablist"
              aria-label="Simulation lab views"
              className="inline-flex max-w-full gap-1 overflow-x-auto rounded-lg border border-runtime-line-soft/60 bg-runtime-panel/60 p-1"
            >
              {SIMULATION_VIEWS.map((view) => (
                <SegmentedButton
                  key={view.id}
                  role="tab"
                  selected={activeView === view.id}
                  onClick={() => setActiveView(view.id)}
                  className="h-7 px-2.5 text-xs"
                >
                  {view.label}
                </SegmentedButton>
              ))}
            </div>
            {nextActionView ? (
              <ToolbarButton
                type="button"
                variant={nextAction.variant}
                size="xs"
                onClick={() => setActiveView(nextActionView)}
              >
                {nextAction.action}
              </ToolbarButton>
            ) : null}
          </>
        }
      />
    </div>
  );
}
