import type { HTMLAttributes, ReactNode } from "react";
import { cx } from "../utils/cx";

export type PanelVariant = "default" | "hover" | "artifact" | "grid" | "industrial";

export type PanelProps = HTMLAttributes<HTMLDivElement> & {
  footer?: ReactNode;
  header?: ReactNode;
  variant?: PanelVariant;
};

const variantClasses: Record<PanelVariant, string> = {
  default: "rounded-runtime-lg border border-runtime-line bg-runtime-soft",
  hover:
    "rounded-runtime-lg border border-runtime-line bg-runtime-panel transition-colors duration-150 ease-telemetry hover:border-signal-protocol-strong/35 hover:bg-runtime-raised",
  artifact: "overflow-hidden rounded-runtime-lg border border-runtime-line bg-runtime-soft",
  grid: "grid gap-px overflow-hidden rounded-runtime-lg border border-runtime-line bg-runtime-line",
  industrial: "grid gap-px border border-ink-black bg-ink-black text-ink-black",
};

export function Panel({ children, className, footer, header, variant = "default", ...props }: PanelProps) {
  return (
    <div className={cx(variantClasses[variant], className)} {...props}>
      {header ? (
        <div className="flex min-h-10 items-center justify-between border-b border-runtime-line bg-runtime-panel px-4 py-2 font-mono text-runtime-xs text-ink-faint">
          {header}
        </div>
      ) : null}
      {children}
      {footer ? <div className="border-t border-runtime-line bg-runtime-panel px-4 py-2 font-mono text-runtime-xs text-ink-faint">{footer}</div> : null}
    </div>
  );
}
