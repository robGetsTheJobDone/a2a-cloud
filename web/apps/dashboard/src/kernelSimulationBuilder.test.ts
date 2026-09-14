import { describe, expect, it } from "vitest";
import { buildAgentKernelSimulationSpec, type KernelBuilderAgent } from "./kernelSimulationBuilder";

function agent(name: string, skill = "pursue"): KernelBuilderAgent {
  return {
    name,
    description: `${name} agent`,
    status: "running",
    card: {
      name,
      description: `${name} card`,
      version: "1.0.0",
      skills: [{ name: skill, description: `${skill} skill`, tags: [], input_schema: {} }],
    },
  };
}

describe("buildAgentKernelSimulationSpec", () => {
  it("builds a bounded simulation spec from selected agents", () => {
    const spec = buildAgentKernelSimulationSpec({
      simulationType: "tournament",
      title: "Agent tournament",
      goal: "Score selected agents without live mutation.",
      agents: [agent("alpha-agent", "research"), agent("beta.agent", "review")],
    });

    expect(spec.simulation_type).toBe("tournament");
    expect(spec.title).toBe("Agent tournament");
    expect(spec.actors).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ id: "kernel-orchestrator", role: "coordinator" }),
        expect.objectContaining({ id: "alpha-agent", role: "participant" }),
        expect.objectContaining({ id: "beta.agent", role: "participant" }),
      ]),
    );
    expect(spec.capabilities).toEqual([
      expect.objectContaining({
        id: "cap-kernel-builder",
        owner: "kernel-orchestrator",
        actions: ["call"],
        resources: ["alpha-agent:invoke:research", "beta.agent:invoke:review"],
      }),
    ]);
    expect(spec.policies).toEqual([
      expect.objectContaining({
        id: "owner-allow-builder-calls",
        resources: ["alpha-agent:invoke:research", "beta.agent:invoke:review"],
      }),
    ]);
    expect(spec.invariants).toEqual(
      expect.arrayContaining(["replay_deterministic", "no_active_apply", "no_violations"]),
    );
    expect(JSON.stringify(spec)).not.toContain("active_apply_enabled\":true");
  });

  it("keeps source agent names and skills in metadata", () => {
    const spec = buildAgentKernelSimulationSpec({
      simulationType: "school",
      title: "Cohort",
      goal: "Model capability growth.",
      agents: [agent("mentor", "teach"), agent("learner", "practice")],
    });

    expect(spec.metadata).toEqual(
      expect.objectContaining({
        builder: "dashboard.kernel-template-builder@v1",
        source_agent_names: ["mentor", "learner"],
        source_agent_skills: { mentor: "teach", learner: "practice" },
      }),
    );
    expect(spec.actors).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ id: "mentor", role: "mentor" }),
        expect.objectContaining({ id: "learner", role: "learner" }),
      ]),
    );
  });

  it("makes normalized duplicate agent ids unique", () => {
    const spec = buildAgentKernelSimulationSpec({
      simulationType: "market",
      title: "Duplicate ids",
      goal: "Avoid graph collisions.",
      agents: [agent("alpha agent", "bid"), agent("alpha/agent", "bid")],
    });

    expect(spec.actors).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ id: "alpha-agent" }),
        expect.objectContaining({ id: "alpha-agent-2" }),
      ]),
    );
    expect(spec.capabilities).toEqual([
      expect.objectContaining({
        resources: ["alpha-agent:invoke:bid", "alpha-agent-2:invoke:bid"],
      }),
    ]);
  });

  it("does not emit undefined metric fields", () => {
    const spec = buildAgentKernelSimulationSpec({
      simulationType: "market",
      title: "Clean JSON",
      goal: "Keep dumps stable.",
      agents: [agent("alpha-agent", "bid")],
    });

    expect(JSON.stringify(spec)).not.toContain("capability_growth");
    expect(JSON.stringify(spec)).not.toContain("undefined");
  });

  it("rejects an empty selection", () => {
    expect(() =>
      buildAgentKernelSimulationSpec({
        simulationType: "market",
        title: "Empty",
        goal: "No agents.",
        agents: [],
      }),
    ).toThrow("Choose at least one agent.");
  });
});
