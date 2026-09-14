export function summarizeRuns(runs: Array<{ status: string }>) {
  return runs.reduce(
    (acc, run) => {
      if (run.status === "error" || run.status === "denied") acc.failures += 1;
      return acc;
    },
    { failures: 0 },
  );
}

export function statusColor(status: string) {
  if (status === "complete") return "text-signal-live";
  if (status === "error" || status === "denied") return "text-signal-danger";
  if (status === "running") return "text-signal-peer";
  return "text-ink-muted";
}

export function fmtDate(value: string): string {
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return "unknown";
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}
