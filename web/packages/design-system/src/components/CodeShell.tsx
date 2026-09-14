import type { HTMLAttributes, ReactNode } from "react";
import { cx } from "../utils/cx";

export type CodeShellProps = HTMLAttributes<HTMLDivElement> & {
  children: ReactNode;
  footer?: ReactNode;
  title?: ReactNode;
  withChrome?: boolean;
};

export function CodeShell({ children, className, footer, title, withChrome = true, ...props }: CodeShellProps) {
  return (
    <div className={cx("overflow-hidden rounded-runtime-lg border border-signal-protocol-strong/25 bg-runtime-soft/95 shadow-glow-cyan", className)} {...props}>
      <div className="flex min-h-10 items-center justify-between border-b border-runtime-line bg-runtime-panel px-4 py-3 font-mono text-runtime-xs text-ink-faint">
        {withChrome ? (
          <div className="flex items-center gap-1.5" aria-hidden="true">
            <span className="h-2.5 w-2.5 rounded-full bg-[#ff5f57]" />
            <span className="h-2.5 w-2.5 rounded-full bg-[#ffbd2e]" />
            <span className="h-2.5 w-2.5 rounded-full bg-[#28c840]" />
          </div>
        ) : (
          <span />
        )}
        {title ? <span>{title}</span> : null}
      </div>
      <div className="overflow-x-auto p-5 font-mono text-sm leading-7 text-ink-soft">{children}</div>
      {footer ? <div className="border-t border-runtime-line px-4 py-3 font-mono text-runtime-xs text-ink-faint">{footer}</div> : null}
    </div>
  );
}
