import type { HTMLAttributes, ReactNode } from "react";
import { StatusPill, cx, type StatusPillProps } from "@a2a/design-system";

/**
 * DashboardSurfacePosture — a compact, one-line surface header strip.
 *
 * Replaces the oversized banner/status panels scattered across the dashboard
 * (mandate D: vertical space is sacred). Pure presentational primitive — no
 * data fetching, no state. Aesthetic is the design-system "runtime telemetry"
 * substrate (runtime.* surfaces, signal.* accents, ink.* ramp, JetBrains Mono
 * for values).
 *
 * Layout: a single horizontal strip that wraps gracefully on narrow viewports.
 *   [ eyebrow / title  •  StatusPill ]  [ metric · metric · metric ]  [ actions ]
 */

export type SurfacePostureMetricTone =
  | "neutral"
  | "live"
  | "authority"
  | "danger"
  | "peer"
  | "proof";

export type SurfacePostureMetric = {
  /** Short uppercase descriptor, e.g. "live" / "failures". */
  label: string;
  /** The figure or short text, mono-rendered. */
  value: ReactNode;
  /** Accent applied to the value; defaults to ink. */
  tone?: SurfacePostureMetricTone;
};

export type SurfacePostureStatus = {
  label: ReactNode;
  tone?: StatusPillProps["tone"];
  pulse?: boolean;
  dot?: boolean;
};

export type DashboardSurfacePostureProps = Omit<
  HTMLAttributes<HTMLDivElement>,
  "title"
> & {
  /** Faint kicker above (or inline before) the title. */
  eyebrow?: ReactNode;
  /** Primary surface label. */
  title: ReactNode;
  /** Optional single status pill rendered beside the title. */
  status?: SurfacePostureStatus;
  /** Inline row of compact telemetry figures. */
  metrics?: SurfacePostureMetric[];
  /** Right-aligned actions slot (buttons, links, menus). */
  actions?: ReactNode;
};

const metricToneClasses: Record<SurfacePostureMetricTone, string> = {
  neutral: "text-ink",
  live: "text-signal-live",
  authority: "text-signal-authority",
  danger: "text-signal-danger",
  peer: "text-signal-peer",
  proof: "text-signal-proof",
};

export function DashboardSurfacePosture({
  eyebrow,
  title,
  status,
  metrics,
  actions,
  className,
  ...divProps
}: DashboardSurfacePostureProps) {
  const hasMetrics = Array.isArray(metrics) && metrics.length > 0;

  return (
    <div
      {...divProps}
      className={cx(
        "flex flex-wrap items-center gap-x-4 gap-y-2 rounded-xl border border-runtime-line-soft/70 bg-runtime-panel/60 px-3.5 py-2",
        className,
      )}
    >
      <div className="flex min-w-0 items-center gap-2.5">
        <div className="min-w-0">
          {eyebrow ? (
            <div className="text-[10px] font-medium uppercase tracking-[0.12em] text-ink-faint">
              {eyebrow}
            </div>
          ) : null}
          <div className="truncate text-runtime-sm font-semibold text-ink">
            {title}
          </div>
        </div>
        {status ? (
          <StatusPill
            tone={status.tone}
            size="xs"
            pulse={status.pulse}
            dot={status.dot ?? true}
          >
            {status.label}
          </StatusPill>
        ) : null}
      </div>

      {hasMetrics ? (
        <dl className="flex min-w-0 flex-wrap items-center gap-x-4 gap-y-1.5">
          {metrics!.map((metric, index) => (
            <div
              key={`${metric.label}-${index}`}
              className="flex items-baseline gap-1.5"
            >
              <dt className="text-[10px] font-medium uppercase tracking-[0.1em] text-ink-faint">
                {metric.label}
              </dt>
              <dd
                className={cx(
                  "font-mono text-runtime-sm tabular-nums",
                  metricToneClasses[metric.tone ?? "neutral"],
                )}
              >
                {metric.value}
              </dd>
            </div>
          ))}
        </dl>
      ) : null}

      {actions ? (
        <div className="ml-auto flex shrink-0 flex-wrap items-center gap-2">
          {actions}
        </div>
      ) : null}
    </div>
  );
}
