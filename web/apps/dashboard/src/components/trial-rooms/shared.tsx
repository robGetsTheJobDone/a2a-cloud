import { useState } from "react";
import type { ReactNode } from "react";
import {
  fetchFile,
  type SubagentFileOp,
  type TrialRoom,
  type TrialRun,
} from "../../api";
import {
  InlineAlert,
  SummaryMetric,
  SurfacePanel,
  ToolbarButton,
} from "../DashboardChrome";
import {
  fmtBytes,
  opTone,
  outputCoverage,
  receiptId,
  stringReceiptValue,
} from "../trialRoomUtils";
import { changedFileCount, fmtMs } from "./helpers";

export function TrialWinnerPanel({ winner }: { winner: TrialRun }) {
  return (
    <SurfacePanel
      as="section"
      className="border-signal-live/45 bg-signal-live/12 p-4"
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="text-xs uppercase text-signal-live">
            Selected winner
          </div>
          <div className="mt-2 font-mono text-sm text-ink">
            {winner.agent_name}.{winner.skill_name}
          </div>
          <div className="mt-1 text-xs text-ink-dim">
            Score {winner.score}/100 · receipt{" "}
            <span className="font-mono">
              {receiptId(winner).slice(0, 16) || "-"}
            </span>
          </div>
        </div>
        <div className="grid w-full grid-cols-2 gap-2 text-xs sm:w-auto sm:min-w-[220px]">
          <SummaryMetric label="outputs" value={changedFileCount(winner)} />
          <SummaryMetric label="runtime" value={fmtMs(winner.elapsed_ms)} />
        </div>
      </div>
    </SurfacePanel>
  );
}

export function ComparisonCell({
  label,
  children,
}: {
  label: string;
  children: ReactNode;
}) {
  return (
    <div className="rounded-md border border-runtime-line-soft/60 bg-runtime-panel/40 px-3 py-2 md:border-0 md:bg-transparent md:p-0">
      <div className="mb-1 text-[10px] uppercase text-ink-faint md:hidden">
        {label}
      </div>
      <div className="min-w-0">{children}</div>
    </div>
  );
}

export function RunEvidenceStrip({ room, run }: { room: TrialRoom; run: TrialRun }) {
  const coverage = outputCoverage(room, run);
  const receipt = receiptId(run);
  const inputHash = stringReceiptValue(run, ["input_set_hash"]);

  return (
    <div className="mt-3 grid gap-2 text-xs sm:grid-cols-3">
      <SummaryMetric
        label="Receipt"
        value={receipt ? receipt.slice(0, 16) : "-"}
        tone={receipt ? "neutral" : "amber"}
        size="compact"
      />
      <SummaryMetric
        label="Output schema"
        value={
          coverage.required.length === 0
            ? "no required keys"
            : coverage.missing.length === 0
            ? `${coverage.present.length}/${coverage.required.length} keys`
            : `missing ${coverage.missing.join(", ")}`
        }
        tone={coverage.missing.length ? "amber" : "emerald"}
        size="compact"
      />
      <SummaryMetric
        label="Input set"
        value={inputHash ? inputHash.slice(0, 16) : `${room.input_paths.length} files`}
        tone="neutral"
        size="compact"
      />
    </div>
  );
}

export function FileOps({ ops }: { ops: SubagentFileOp[] }) {
  const [err, setErr] = useState<string | null>(null);

  async function download(path: string) {
    try {
      const blob = await fetchFile(path);
      const url = URL.createObjectURL(blob);
      try {
        const a = document.createElement("a");
        a.href = url;
        a.download = path.split("/").pop() || path;
        document.body.appendChild(a);
        a.click();
        a.remove();
      } finally {
        URL.revokeObjectURL(url);
      }
    } catch (ex) {
      setErr(ex instanceof Error ? ex.message : String(ex));
    }
  }

  return (
    <SurfacePanel as="div" className="mt-3 bg-runtime-panel/50 p-2">
      <div className="mb-1 text-[10px] uppercase text-ink-faint">
        output files
      </div>
      <div className="space-y-1 font-mono text-xs">
        {ops.map((op, i) => (
          <div
            key={`${op.path}-${i}`}
            className="flex min-w-0 flex-wrap items-center gap-2"
          >
            <span className={opTone(op.op)}>{op.op}</span>
            <span className="min-w-0 flex-1 truncate text-ink-soft">{op.path}</span>
            <span className="text-ink-faint">{fmtBytes(op.size)}</span>
            {op.op !== "delete" && (
              <ToolbarButton
                onClick={() => download(op.path)}
                variant="ghost"
                size="xs"
              >
                download
              </ToolbarButton>
            )}
          </div>
        ))}
      </div>
      {err && (
        <InlineAlert tone="red" role="alert" className="mt-2 text-xs">
          {err}
        </InlineAlert>
      )}
    </SurfacePanel>
  );
}
