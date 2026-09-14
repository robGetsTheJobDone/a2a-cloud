import type { HTMLAttributes, ReactNode } from "react";
import { cx } from "../utils/cx";

export type StatusPillTone =
  | "protocol"
  | "live"
  | "authority"
  | "peer"
  | "proof"
  | "danger"
  | "neutral";

export type StatusPillSize = "xs" | "sm" | "md";

export type StatusPillProps = HTMLAttributes<HTMLSpanElement> & {
  children: ReactNode;
  tone?: StatusPillTone;
  size?: StatusPillSize;
  pulse?: boolean;
  dot?: boolean;
};

const toneClasses: Record<StatusPillTone, string> = {
  protocol: "border-signal-protocol-strong/35 bg-signal-protocol-strong/10 text-signal-protocol",
  live: "border-signal-live/35 bg-signal-live/10 text-signal-live",
  authority: "border-signal-authority/35 bg-signal-authority/10 text-signal-authority",
  peer: "border-signal-peer/35 bg-signal-peer/10 text-signal-peer",
  proof: "border-signal-proof/35 bg-signal-proof/10 text-signal-proof",
  danger: "border-signal-danger/40 bg-signal-danger/12 text-signal-danger",
  neutral: "border-runtime-line-mid bg-runtime-panel text-ink-soft",
};

const dotClasses: Record<StatusPillTone, string> = {
  protocol: "bg-signal-protocol shadow-[0_0_8px_rgba(126,231,208,0.6)]",
  live: "bg-signal-live shadow-glow-live",
  authority: "bg-signal-authority shadow-[0_0_8px_rgba(243,211,107,0.55)]",
  peer: "bg-signal-peer shadow-glow-peer",
  proof: "bg-signal-proof shadow-[0_0_8px_rgba(255,141,179,0.55)]",
  danger: "bg-signal-danger shadow-[0_0_8px_rgba(255,92,122,0.55)]",
  neutral: "bg-ink-faint",
};

const sizeClasses: Record<StatusPillSize, string> = {
  xs: "gap-1.5 px-2 py-0.5 text-[10px]",
  sm: "gap-2 px-2.5 py-1 text-runtime-xs",
  md: "gap-2 px-3 py-1.5 text-runtime-sm",
};

export function StatusPill({
  children,
  className,
  tone = "protocol",
  size = "sm",
  pulse = false,
  dot = true,
  ...props
}: StatusPillProps) {
  return (
    <span
      className={cx(
        "inline-flex items-center rounded-full border font-mono tracking-[0.04em]",
        toneClasses[tone],
        sizeClasses[size],
        className,
      )}
      {...props}
    >
      {dot ? (
        <span className="relative inline-flex h-1.5 w-1.5 shrink-0">
          {pulse ? (
            <span
              className={cx(
                "absolute inline-flex h-full w-full animate-graph-ping rounded-full opacity-75",
                dotClasses[tone],
              )}
            />
          ) : null}
          <span className={cx("relative inline-flex h-1.5 w-1.5 rounded-full", dotClasses[tone])} />
        </span>
      ) : null}
      {children}
    </span>
  );
}
