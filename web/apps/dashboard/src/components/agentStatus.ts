export function isActiveDeploymentStatus(status?: string | null): boolean {
  return (
    status === "queued" ||
    status === "building" ||
    status === "deploying" ||
    status === "verifying"
  );
}

export function isTransientAgentStatus(status?: string | null): boolean {
  return [
    "building",
    "deploying",
    "pending",
    "provisioning",
    "queued",
    "verifying",
  ].includes(String(status || "").toLowerCase());
}
