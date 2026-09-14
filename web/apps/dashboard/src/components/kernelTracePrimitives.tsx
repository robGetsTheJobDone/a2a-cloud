import {
  SummaryMetric,
} from "./DashboardChrome";
import { StatusBadge } from "./StatusPillAdapters";

// Small, dependency-light kernel-trace primitives kept in their own module so
// that evidence cards (ChatEvidenceCards) can import them WITHOUT pulling in the
// heavy lazy KernelTraceCard body. KernelTraceCard re-exports these statically so
// existing import sites stay stable.

function traceToneToBadgeTone(
  tone: "cyan" | "emerald" | "amber" | "red" | "neutral",
) {
  return tone === "cyan" ? "neutral" : tone;
}

export function KernelTraceBadge({
  label,
  tone,
}: {
  label: string;
  tone: "cyan" | "emerald" | "amber" | "red" | "neutral";
}) {
  return (
    <StatusBadge
      tone={traceToneToBadgeTone(tone)}
      className="max-w-full rounded-md text-[10px]"
    >
      <span className="truncate">{label}</span>
    </StatusBadge>
  );
}

export function KernelTraceStat({
  label,
  value,
  tone,
}: {
  label: string;
  value: string | number;
  tone?: "emerald" | "red";
}) {
  return (
    <SummaryMetric
      label={label}
      value={value}
      tone={tone || "neutral"}
      size="compact"
      className="bg-runtime-bg/70 px-2 py-1 text-left"
    />
  );
}

export function KernelTraceGraphNode({
  label,
  value,
  tone,
}: {
  label: string;
  value: string;
  tone: "cyan" | "emerald" | "amber" | "red";
}) {
  return (
    <SummaryMetric
      label={label}
      value={value}
      tone={traceToneToBadgeTone(tone)}
      size="compact"
      className="px-3 py-2"
    />
  );
}

export function KernelTraceSegment({
  label,
  value,
  tone,
}: {
  label: string;
  value: string | number;
  tone: "cyan" | "emerald" | "amber" | "red" | "neutral";
}) {
  return (
    <StatusBadge
      tone={traceToneToBadgeTone(tone)}
      className="shrink-0 rounded-md bg-runtime-bg/80 text-[10px] font-normal"
    >
      <span className="text-ink-faint">{label}</span>
      <span className="font-mono">{value}</span>
    </StatusBadge>
  );
}

export function KernelTraceRail() {
  return <span className="h-px min-w-3 flex-1 bg-runtime-line/80" />;
}
