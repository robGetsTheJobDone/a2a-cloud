import { describe, expect, it } from "vitest";
import { isActiveDeploymentStatus, isTransientAgentStatus } from "./agentStatus";

describe("agent status helpers", () => {
  it("identifies active deployment statuses", () => {
    expect(isActiveDeploymentStatus("queued")).toBe(true);
    expect(isActiveDeploymentStatus("building")).toBe(true);
    expect(isActiveDeploymentStatus("deploying")).toBe(true);
    expect(isActiveDeploymentStatus("verifying")).toBe(true);
    expect(isActiveDeploymentStatus("live")).toBe(false);
    expect(isActiveDeploymentStatus("failed")).toBe(false);
    expect(isActiveDeploymentStatus("Deploying")).toBe(false);
    expect(isActiveDeploymentStatus(null)).toBe(false);
  });

  it("identifies transient agent statuses case-insensitively", () => {
    expect(isTransientAgentStatus("pending")).toBe(true);
    expect(isTransientAgentStatus("Provisioning")).toBe(true);
    expect(isTransientAgentStatus("ready")).toBe(false);
    expect(isTransientAgentStatus(undefined)).toBe(false);
  });
});
