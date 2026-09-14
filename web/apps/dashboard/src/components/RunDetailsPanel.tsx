import { lazy, Suspense, useId, useState } from "react";
import {
  type ControlReceipt,
  type ControlTimelineItem,
  type SubagentFileOp,
  type SubagentGrant,
  type SubagentHandoff,
  type SubagentReceipt,
} from "../api";
import {
  CodeBlock,
  InlineAlert,
  SummaryMetric,
  SurfacePanel,
} from "./DashboardChrome";
import { StateBadge } from "./StatusPillAdapters";
import { WorkspaceArtifactList } from "./WorkspaceArtifactPreview";

/**
 * The receipt view is code-split: this panel is in the entry chunk (chat rail,
 * agent runs, workspace activity all import it), but only runs that actually
 * carry sealed evidence pay for the decoder — and only a click pays for the
 * Ed25519 check inside it.
 */
const ReceiptEvidence = lazy(() =>
  import("./ReceiptEvidence").then((module) => ({ default: module.ReceiptEvidence })),
);

type RunDetailEvent = {
  id: string | number;
  type: string;
  payload: Record<string, unknown>;
  created_at?: string | null;
};

type RunDetailNode = {
  id: string;
  agent: string;
  skill: string;
  status: string;
  summary?: string | null;
  error?: string | null;
  grant_id?: string | null;
  elapsed_ms?: number | null;
  file_ops?: SubagentFileOp[];
  args?: Record<string, unknown>;
  result?: Record<string, unknown> | null;
};

export type RunDetails = {
  kind: "subagent" | "dag" | "llm" | "trial" | "proof" | "agent-log";
  id: string;
  title: string;
  subtitle?: string;
  status: string;
  summary?: string | null;
  error?: string | null;
  stats?: { label: string; value: string }[];
  args?: Record<string, unknown> | null;
  result?: unknown;
  scopes?: Record<string, unknown> | null;
  file_ops?: SubagentFileOp[];
  original_request?: unknown;
  handoffs?: SubagentHandoff[];
  grants?: SubagentGrant[];
  receipts?: SubagentReceipt[];
  events?: RunDetailEvent[];
  nodes?: RunDetailNode[];
  receipt?: unknown;
};

export type RunDetailsView = "all" | "overview" | "files" | "payload" | "timeline";

export function RunDetailsPanel({
  details,
  loading = false,
  error,
  className = "",
  view = "all",
}: {
  details: RunDetails | null;
  loading?: boolean;
  error?: string | null;
  className?: string;
  view?: RunDetailsView;
}) {
  return (
    <div className={className}>
      {loading && (
        <div
          role="status"
          className="py-12 text-center text-sm text-ink-muted"
        >
          Loading run...
        </div>
      )}
      {error && (
        <InlineAlert tone="red" role="alert" className="mb-4">
          {error}
        </InlineAlert>
      )}
      {details && (
        <div className="space-y-4">
          {(view === "all" || view === "overview") && (
            <RunOverviewSection details={details} quietEmpty={view === "all"} />
          )}

          {(view === "all" || view === "files") && (
            <RunFilesSection details={details} quietEmpty={view === "all"} />
          )}

          {(view === "all" || view === "payload") && (
            <RunPayloadSection details={details} quietEmpty={view === "all"} />
          )}

          {(view === "all" || view === "timeline") && (
            <RunTimelineSection details={details} quietEmpty={view === "all"} />
          )}
        </div>
      )}
    </div>
  );
}

function RunOverviewSection({
  details,
  quietEmpty,
}: {
  details: RunDetails;
  quietEmpty: boolean;
}) {
  return (
    <>
      <div className="grid gap-2 text-xs text-ink-dim md:grid-cols-4">
        <SummaryMetric label="status" value={details.status} />
        <SummaryMetric
          label="id"
          value={<span title={details.id}>{details.id}</span>}
        />
        {(details.stats || []).map((stat) => (
          <SummaryMetric key={stat.label} label={stat.label} value={stat.value} />
        ))}
      </div>

      {details.error ? (
        <InlineAlert tone="red" role="alert">
          {details.error}
        </InlineAlert>
      ) : details.summary ? (
        <SurfacePanel
          as="div"
          className="bg-runtime-panel/50 p-3 text-sm text-ink-soft [overflow-wrap:anywhere]"
        >
          {details.summary}
        </SurfacePanel>
      ) : quietEmpty ? null : (
        <SurfacePanel as="div" className="bg-runtime-panel/40 p-3 text-xs text-ink-muted">
          No run summary was recorded.
        </SurfacePanel>
      )}
    </>
  );
}

function RunFilesSection({
  details,
  quietEmpty,
}: {
  details: RunDetails;
  quietEmpty: boolean;
}) {
  if (details.file_ops && details.file_ops.length > 0) {
    return <RunFileOps ops={details.file_ops} defaultOpen />;
  }
  if (quietEmpty) return null;
  return (
    <SurfacePanel as="div" className="bg-runtime-panel/40 p-3 text-xs text-ink-muted">
      This run did not report any file operations.
    </SurfacePanel>
  );
}

function RunPayloadSection({
  details,
  quietEmpty,
}: {
  details: RunDetails;
  quietEmpty: boolean;
}) {
  const blocks: Array<{ title: string; value: unknown }> = [];
  if (details.original_request !== undefined) {
    blocks.push({ title: "original request", value: details.original_request });
  }
  if (details.args) blocks.push({ title: "args", value: details.args });
  if (details.result !== undefined) blocks.push({ title: "result", value: details.result });
  if (details.scopes) blocks.push({ title: "scopes", value: details.scopes });
  if (details.receipt !== undefined) blocks.push({ title: "receipt", value: details.receipt });
  if (details.handoffs && details.handoffs.length > 0) {
    blocks.push({ title: "handoffs", value: details.handoffs });
  }
  if (details.grants && details.grants.length > 0) {
    blocks.push({ title: "grants", value: details.grants });
  }
  // Receipts are deliberately NOT a JSON block: a signed receipt is the one
  // artifact here an owner can check for themselves, so it gets a real detail
  // view (ReceiptEvidence) rather than a pretty-printed blob.
  const receipts = details.receipts || [];

  if (blocks.length === 0 && receipts.length === 0) {
    return quietEmpty ? null : (
      <SurfacePanel as="div" className="bg-runtime-panel/40 p-3 text-xs text-ink-muted">
        No structured payload was recorded for this run.
      </SurfacePanel>
    );
  }

  return (
    <div className="grid gap-4">
      {receipts.length > 0 && (
        <Suspense
          fallback={
            <SurfacePanel as="div" className="bg-runtime-panel/40 p-3 text-xs text-ink-muted">
              Loading signed evidence...
            </SurfacePanel>
          }
        >
          <ReceiptEvidence receipts={receipts} />
        </Suspense>
      )}
      {blocks.length > 0 && (
        <div className="grid gap-4 lg:grid-cols-2">
          {blocks.map((block) => (
            <JsonBlock key={block.title} title={block.title} value={block.value} />
          ))}
        </div>
      )}
    </div>
  );
}

function RunTimelineSection({
  details,
  quietEmpty,
}: {
  details: RunDetails;
  quietEmpty: boolean;
}) {
  const hasNodes = Boolean(details.nodes && details.nodes.length > 0);
  const hasEvents = Boolean(details.events && details.events.length > 0);

  if (!hasNodes && !hasEvents) {
    return quietEmpty ? null : (
      <SurfacePanel as="div" className="bg-runtime-panel/40 p-3 text-xs text-ink-muted">
        No timeline events were recorded for this run.
      </SurfacePanel>
    );
  }

  return (
    <>
      {hasNodes && (
        <SurfacePanel as="section" className="overflow-hidden bg-runtime-panel/40">
          <SectionTitle>nodes</SectionTitle>
          <div className="divide-y divide-runtime-line-soft">
            {details.nodes?.map((node) => (
              <NodeRow key={node.id} node={node} />
            ))}
          </div>
        </SurfacePanel>
      )}

      {hasEvents && (
        <SurfacePanel as="section" className="bg-runtime-panel/40">
          <SectionTitle>timeline</SectionTitle>
          <div className="space-y-2 p-3">
            {details.events?.map((event) => (
              <TimelineEvent key={event.id} event={event} />
            ))}
          </div>
        </SurfacePanel>
      )}
    </>
  );
}

function RunFileOps({
  ops,
  defaultOpen = false,
}: {
  ops: SubagentFileOp[];
  defaultOpen?: boolean;
}) {
  const panelId = useId();
  const [open, setOpen] = useState(defaultOpen);
  const counts = ops.reduce<Record<string, number>>((acc, op) => {
    acc[op.op] = (acc[op.op] || 0) + 1;
    return acc;
  }, {});
  const summary = ["create", "update", "delete"]
    .filter((op) => counts[op])
    .map((op) => `${counts[op]} ${op}${counts[op] > 1 ? "s" : ""}`)
    .join(", ");

  return (
    <SurfacePanel as="section" className="min-w-0 bg-runtime-panel/50">
      <button
        type="button"
        onClick={() => setOpen((next) => !next)}
        aria-expanded={open}
        aria-controls={panelId}
        aria-label={`${open ? "Hide" : "Show"} touched files${
          summary ? `: ${summary}` : ""
        }`}
        className="flex w-full min-w-0 items-center gap-2 px-3 py-2 text-left text-xs text-ink-dim hover:bg-runtime-panel hover:text-ink-soft focus-visible:outline focus-visible:outline-2 focus-visible:outline-inset focus-visible:outline-signal-protocol"
      >
        <span aria-hidden="true" className="w-3 shrink-0">
          {open ? "v" : ">"}
        </span>
        <span className="shrink-0 font-medium text-ink-soft">
          Files touched
        </span>
        {summary && (
          <span className="min-w-0 truncate text-ink-muted">({summary})</span>
        )}
      </button>
      {open && (
        <div
          id={panelId}
          className="border-t border-runtime-line-soft/60 p-2"
        >
          <WorkspaceArtifactList
            artifacts={ops}
            variant="list"
            showDeleted
          />
        </div>
      )}
    </SurfacePanel>
  );
}

function NodeRow({ node }: { node: RunDetailNode }) {
  const hasDetails = Boolean(node.args) || node.result !== undefined;
  return (
    <div className="min-w-0 p-3 text-xs">
      <div className="flex min-w-0 flex-wrap items-center gap-2">
        <StateBadge status={node.status} size="xs" />
        <span className="min-w-0 font-mono text-ink [overflow-wrap:anywhere]">
          {node.id}
        </span>
        <span className="text-ink-faint">·</span>
        <span className="min-w-0 font-mono text-ink-soft [overflow-wrap:anywhere]">
          {node.agent}.{node.skill}
        </span>
        {node.elapsed_ms !== null && node.elapsed_ms !== undefined && (
          <span className="text-ink-faint">{fmtMs(node.elapsed_ms)}</span>
        )}
        {node.grant_id && (
          <span className="text-ink-muted">
            grant <span className="font-mono">{node.grant_id.slice(0, 8)}</span>
          </span>
        )}
      </div>
      {node.summary && (
        <div className="mt-2 text-ink-dim [overflow-wrap:anywhere]">
          <span className="text-ink-faint">
            {node.error ? "error " : "result "}
          </span>
          {node.summary}
        </div>
      )}
      {node.error && node.error !== node.summary && (
        <InlineAlert tone="red" role="alert" className="mt-2 text-xs [overflow-wrap:anywhere]">
          {node.error}
        </InlineAlert>
      )}
      {node.file_ops && node.file_ops.length > 0 && (
        <div className="mt-2">
          <RunFileOps ops={node.file_ops} />
        </div>
      )}
      {hasDetails && (
        <details className="mt-2 min-w-0 rounded-md border border-runtime-line-soft/60 bg-runtime-bg">
          <summary className="cursor-pointer px-2 py-1 text-ink-muted hover:text-ink-soft focus-visible:outline focus-visible:outline-2 focus-visible:outline-inset focus-visible:outline-signal-protocol">
            Node data
          </summary>
          <div className="grid min-w-0 gap-2 border-t border-runtime-line-soft/60 p-2 md:grid-cols-2">
            {node.args && <JsonBlock title="args" value={node.args} />}
            {node.result !== undefined && (
              <JsonBlock title="result" value={node.result} />
            )}
          </div>
        </details>
      )}
    </div>
  );
}

function TimelineEvent({ event }: { event: RunDetailEvent }) {
  const payload = cleanPayload(event.payload);
  const err = eventPayloadError(payload);
  const summary = err || eventPayloadSummary(payload);
  return (
    <details
      open={Boolean(err)}
      className={`min-w-0 rounded-md border bg-runtime-bg text-xs ${
        err ? "border-signal-danger/50" : "border-runtime-line-soft/60"
      }`}
    >
      <summary className="cursor-pointer list-none p-2 focus-visible:outline focus-visible:outline-2 focus-visible:outline-inset focus-visible:outline-signal-protocol">
        <div className="grid min-w-0 gap-2 sm:grid-cols-[minmax(0,1fr)_auto]">
          <div className="flex min-w-0 items-start gap-2">
            <StateBadge status={eventStatus(event, payload)} size="xs" />
            <span className="min-w-0">
              <span className="block font-mono text-ink-soft [overflow-wrap:anywhere]">
                {event.type}
              </span>
              <span
                className={
                  "mt-0.5 block [overflow-wrap:anywhere] " +
                  (err ? "text-signal-danger" : "text-ink-muted")
                }
              >
                {summary}
              </span>
            </span>
          </div>
          {event.created_at && (
            <span className="shrink-0 text-ink-faint">
              {new Date(event.created_at).toLocaleString()}
            </span>
          )}
        </div>
      </summary>
      <CodeBlock
        tabIndex={0}
        aria-label={`${event.type} event payload`}
        className="max-h-48 max-w-full rounded-none border-x-0 border-b-0 bg-transparent p-2 text-[11px] text-ink-muted [overflow-wrap:anywhere] focus-visible:outline focus-visible:outline-2 focus-visible:outline-inset focus-visible:outline-signal-protocol"
      >
        {JSON.stringify(payload, null, 2)}
      </CodeBlock>
    </details>
  );
}

function eventStatus(event: RunDetailEvent, payload: Record<string, unknown>) {
  if (eventPayloadError(payload)) return "error";
  if (payload.ok === false) return "error";
  if (event.type.includes("complete") || event.type.includes("submitted")) {
    return "complete";
  }
  if (event.type.includes("denied")) return "denied";
  if (event.type.includes("started") || event.type.includes("progress")) {
    return "running";
  }
  return String(payload.status || "pending");
}

function eventPayloadSummary(payload: Record<string, unknown>) {
  for (const key of ["summary", "message", "reason"]) {
    const value = payload[key];
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  const a2a = payload.a2a;
  if (a2a && typeof a2a === "object" && !Array.isArray(a2a)) {
    const status = (a2a as Record<string, unknown>).status_message;
    if (typeof status === "string" && status.trim()) return status.trim();
  }
  const type = payload.type;
  return typeof type === "string" ? type : "event";
}

function eventPayloadError(payload: Record<string, unknown>) {
  for (const key of ["error", "error_message"]) {
    const value = payload[key];
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  if (payload.ok === false) return eventPayloadSummary(payload);
  const a2a = payload.a2a;
  if (a2a && typeof a2a === "object" && !Array.isArray(a2a)) {
    const record = a2a as Record<string, unknown>;
    if (record.task_status === "error" || record.task_status === "canceled") {
      const message = record.status_message;
      return typeof message === "string" && message.trim()
        ? message.trim()
        : String(record.task_status);
    }
  }
  return null;
}

function JsonBlock({ title, value }: { title: string; value: unknown }) {
  const titleId = useId();
  return (
    <section aria-labelledby={titleId} className="min-w-0">
      <div
        id={titleId}
        className="mb-1 text-[10px] uppercase text-ink-faint"
      >
        {title}
      </div>
      <CodeBlock
        tabIndex={0}
        aria-label={`${title} JSON`}
        className="max-h-72 max-w-full text-[11px] text-ink-dim [overflow-wrap:anywhere] focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-signal-protocol"
      >
        {JSON.stringify(value, null, 2)}
      </CodeBlock>
    </section>
  );
}

function SectionTitle({ children }: { children: string }) {
  return (
    <div className="border-b border-runtime-line-soft/60 px-3 py-2 text-[10px] uppercase text-ink-faint">
      {children}
    </div>
  );
}

/**
 * Map a runtime ControlReceipt (and the ledger event that triggered it) onto the
 * shared RunDetails shape so the control-room receipt sheet renders through the
 * SAME generic body as agent/dag/llm/trial runs instead of a forked panel.
 *
 * - overview  -> status, receipt id, cost/token/file stats, summary
 * - files     -> receipt.file_ops
 * - payload   -> args_preview, result_preview, ids, scopes, costs, metadata,
 *                events (everything the old "payload" + "metadata" tabs showed)
 * - timeline  -> receipt.events (raw runtime events)
 */
export function controlReceiptDetails(
  receipt: ControlReceipt,
  trigger?: ControlTimelineItem | null,
): RunDetails {
  const costUsd = Number(receipt.costs?.cost_usd || 0);
  const totalTokens = Number(receipt.costs?.total_tokens || 0);
  const stats: { label: string; value: string }[] = [
    { label: "source", value: receipt.source },
    { label: "cost", value: `$${costUsd.toFixed(4)}` },
    { label: "tokens", value: String(totalTokens) },
    { label: "files", value: String(receipt.file_ops.length) },
    { label: "created", value: new Date(receipt.created_at).toLocaleString() },
  ];
  if (receipt.completed_at) {
    stats.push({
      label: "completed",
      value: new Date(receipt.completed_at).toLocaleString(),
    });
  }
  return {
    kind: "proof",
    id: receipt.receipt_id,
    title: receipt.subject,
    subtitle: trigger?.title || undefined,
    status: receipt.status,
    summary: receipt.summary || trigger?.summary || null,
    stats,
    args: receipt.args_preview,
    result: receipt.result_preview,
    scopes: receipt.scopes,
    file_ops: receipt.file_ops,
    receipt: {
      ids: receipt.ids,
      costs: receipt.costs,
      metadata: receipt.metadata,
    },
    events: receipt.events.map((event, index) => ({
      id: (event.id as string | number) ?? index,
      type: String(event.type || event.event_type || "event"),
      payload: event,
      created_at: (event.created_at as string | undefined) ?? null,
    })),
  };
}

function cleanPayload(payload: Record<string, unknown>) {
  const out: Record<string, unknown> = { ...payload };
  delete out.args_json;
  if (
    out.handoff &&
    typeof out.handoff === "object" &&
    !Array.isArray(out.handoff)
  ) {
    const handoff = { ...(out.handoff as Record<string, unknown>) };
    delete handoff.args_json;
    out.handoff = handoff;
  }
  return out;
}

function fmtMs(ms: number) {
  if (ms < 1000) return `${ms}ms`;
  return `${Math.round(ms / 100) / 10}s`;
}
