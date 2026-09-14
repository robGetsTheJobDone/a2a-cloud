import { describe, expect, it } from "vitest";
import type { KernelEvolutionRun } from "../../api";
import {
  analyzeSimulationSpecReadiness,
  hasSelectedLiveAgents,
  mergeRunByJobId,
  numberValue,
  resultVariants,
  runResult,
  simulationKindLabel,
  textValue,
} from "./simulationModel";

describe("simulationModel", () => {
  it("formats compact simulation labels", () => {
    expect(simulationKindLabel("money_network_evolution")).toBe("money network evolution");
  });

  it("detects specs built from selected live agents", () => {
    expect(
      hasSelectedLiveAgents({
        metadata: { source_agent_names: ["allocator", ""] },
      }),
    ).toBe(true);
    expect(hasSelectedLiveAgents({ metadata: { source_agent_names: [" "] } })).toBe(false);
    expect(hasSelectedLiveAgents({ metadata: null })).toBe(false);
  });

  it("reads evolution run result payloads and filters variants", () => {
    const run = {
      job: {
        result: {
          winner_variant_id: "variant-a",
          variants: [{ variant_id: "variant-a" }, null, 42, ["skip"]],
        },
      },
    } as unknown as KernelEvolutionRun;

    const result = runResult(run);

    expect(result?.winner_variant_id).toBe("variant-a");
    expect(resultVariants(result)).toEqual([{ variant_id: "variant-a" }]);
  });

  it("falls back for untrusted text and number values", () => {
    expect(textValue("winner", "-")).toBe("winner");
    expect(textValue(" ", "-")).toBe("-");
    expect(numberValue(4, 0)).toBe(4);
    expect(numberValue(Number.NaN, 7)).toBe(7);
  });

  it("merges directly loaded runs without duplicating job ids", () => {
    const existing = [
      { job: { job_id: "run-1" }, events: [] },
      { job: { job_id: "run-2" }, events: [] },
    ];
    const next = { job: { job_id: "run-2" }, events: ["fresh"] };

    expect(mergeRunByJobId(existing, next)).toEqual([
      next,
      { job: { job_id: "run-1" }, events: [] },
    ]);
  });

  it("classifies empty and invalid simulation specs before run", () => {
    expect(analyzeSimulationSpecReadiness("", "bounded")).toMatchObject({
      state: "empty",
      canRun: false,
      actorCount: 0,
    });

    expect(analyzeSimulationSpecReadiness("{", "bounded")).toMatchObject({
      state: "invalid",
      canRun: false,
      label: "Invalid JSON",
    });

    expect(analyzeSimulationSpecReadiness("[]", "bounded")).toMatchObject({
      state: "invalid",
      canRun: false,
      label: "Spec must be an object",
    });
  });

  it("summarizes proof-only spec readiness", () => {
    const readiness = analyzeSimulationSpecReadiness(
      JSON.stringify({
        actors: [{ id: "coordinator" }, { id: "reviewer" }],
        steps: [{ type: "start_process" }, { type: "stop_process" }],
        invariants: ["replay_deterministic"],
      }),
      "bounded",
    );

    expect(readiness).toMatchObject({
      state: "ready",
      canRun: true,
      actorCount: 2,
      stepCount: 2,
      invariantCount: 1,
      liveSourceCount: 0,
    });
  });

  it("blocks live-agent mode until selected agent metadata is present", () => {
    expect(
      analyzeSimulationSpecReadiness(
        JSON.stringify({ actors: [], steps: [], invariants: [] }),
        "hybrid",
      ),
    ).toMatchObject({
      state: "blocked",
      canRun: false,
      liveSourceCount: 0,
    });

    expect(
      analyzeSimulationSpecReadiness(
        JSON.stringify({
          actors: [],
          steps: [],
          invariants: [],
          metadata: { source_agent_names: ["allocator"] },
        }),
        "hybrid",
      ),
    ).toMatchObject({
      state: "ready",
      canRun: true,
      liveSourceCount: 1,
    });
  });
});
