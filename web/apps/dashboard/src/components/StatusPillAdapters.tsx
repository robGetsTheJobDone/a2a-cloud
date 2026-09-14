import type { HTMLAttributes, ReactNode } from "react";
import { StatusPill, type StatusPillTone } from "@a2a/design-system";
import {
  isLiveStateStatus,
  stateStatusTone,
  type StatusBadgeTone,
} from "./DashboardChrome";

/**
 * StatusPill adapters.
 *
 * The dashboard historically rendered status chips through a local
 * `StatusBadge`/`StateBadge` fork in DashboardChrome. Those forks are retired
 * in favor of the shared `@a2a/design-system` StatusPill (the "Runtime
 * Telemetry" chip — JetBrains Mono, signal.* accents, runtime.* substrate).
 *
 * These thin adapters preserve the legacy call-site API (the four-value
 * `StatusBadgeTone` vocabulary + the status-string derivation of StateBadge)
 * while delegating all rendering to StatusPill, so the dozens of existing
 * call-sites migrate without prop churn and the tone semantics stay correct.
 */

const badgeToneToPillTone: Record<StatusBadgeTone, StatusPillTone> = {
  neutral: "neutral",
  emerald: "live",
  amber: "authority",
  red: "danger",
};

export type StatusBadgeProps = HTMLAttributes<HTMLSpanElement> & {
  tone?: StatusBadgeTone;
  dot?: boolean;
  children: ReactNode;
};

export function StatusBadge({
  tone = "neutral",
  dot = false,
  children,
  ...spanProps
}: StatusBadgeProps) {
  return (
    <StatusPill
      {...spanProps}
      tone={badgeToneToPillTone[tone]}
      size="xs"
      dot={dot}
    >
      {children}
    </StatusPill>
  );
}

export type StateBadgeProps = Omit<StatusBadgeProps, "tone" | "dot" | "children"> & {
  status: string | null | undefined;
  label?: ReactNode;
  tone?: StatusBadgeTone;
  dot?: boolean;
  live?: boolean;
  size?: "xs" | "sm";
};

export function StateBadge({
  status,
  label,
  tone,
  dot,
  live,
  size = "sm",
  ...spanProps
}: StateBadgeProps) {
  const resolvedLive = live ?? isLiveStateStatus(status);
  const resolvedDot = dot ?? resolvedLive;
  return (
    <StatusPill
      {...spanProps}
      tone={badgeToneToPillTone[tone ?? stateStatusTone(status)]}
      size={size === "xs" ? "xs" : "xs"}
      dot={resolvedDot}
      pulse={resolvedLive}
      role={spanProps.role ?? (resolvedLive ? "status" : undefined)}
      aria-live={spanProps["aria-live"] ?? (resolvedLive ? "polite" : undefined)}
    >
      {label ?? status ?? "unknown"}
    </StatusPill>
  );
}
