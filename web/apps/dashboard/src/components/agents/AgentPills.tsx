import { isActiveDeploymentStatus } from "../myAgentsFleet";
import { StateBadge } from "../StatusPillAdapters";

export function DeploymentPill({ status }: { status: string }) {
  return (
    <StateBadge
      status={status}
      label={`deploy ${status}`}
      live={isActiveDeploymentStatus(status)}
      size="xs"
    />
  );
}

export function RuntimePill() {
  return (
    <StateBadge
      status="runtime_update"
      label="runtime update"
      tone="amber"
      size="xs"
    />
  );
}
