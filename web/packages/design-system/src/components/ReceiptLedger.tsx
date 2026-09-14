import type { HTMLAttributes, ReactNode } from "react";
import { cx } from "../utils/cx";

export type ReceiptGroup = "id" | "call" | "authority" | "ops" | "outcome" | "proof";

export type ReceiptRow = {
  field: ReactNode;
  group: ReceiptGroup;
  value: ReactNode;
};

export type ReceiptLedgerProps = HTMLAttributes<HTMLDivElement> & {
  rows: ReceiptRow[];
  status?: ReactNode;
  title?: ReactNode;
};

const groupClasses: Record<ReceiptGroup, string> = {
  id: "text-signal-protocol",
  call: "text-signal-peer",
  authority: "text-signal-authority",
  ops: "text-amber-500",
  outcome: "text-signal-live",
  proof: "text-signal-proof",
};

export function ReceiptLedger({ className, rows, status = "sealed", title = "receipt.json", ...props }: ReceiptLedgerProps) {
  return (
    <div className={cx("overflow-hidden rounded-runtime-lg border border-signal-live/35 bg-runtime-soft shadow-glow-live", className)} {...props}>
      <div className="flex items-center justify-between border-b border-runtime-line bg-runtime-panel px-4 py-2 font-mono text-runtime-xs text-ink-faint">
        <span>{title}</span>
        <span className="text-signal-live">{status}</span>
      </div>
      <div>
        {rows.map((row, index) => (
          <div
            className="grid grid-cols-[76px_minmax(0,1fr)] items-center gap-x-3 gap-y-1 border-b border-runtime-line px-4 py-2 last:border-b-0 sm:grid-cols-[68px_120px_minmax(0,1fr)]"
            key={`${row.group}-${index}`}
          >
            <span className={cx("font-mono text-[10px] uppercase tracking-wider", groupClasses[row.group])}>{row.group}</span>
            <span className="font-mono text-xs text-ink-dim">{row.field}</span>
            <span className="col-span-2 min-w-0 break-all font-mono text-xs text-ink-soft sm:col-span-1 sm:truncate">{row.value}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
