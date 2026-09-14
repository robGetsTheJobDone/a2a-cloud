import {
  EmptyState,
  InlineAlert,
  LoadingState,
  SegmentedControl,
  SummaryMetric,
  SurfacePanel,
  TabLink,
  ToolbarLink,
} from "../DashboardChrome";
import { StateBadge } from "../StatusPillAdapters";
import { DetailSheet } from "../ListDetailLayout";
import { KernelTraceCard } from "../KernelTraceCard";
import { RunDetailsPanel, controlReceiptDetails } from "../RunDetailsPanel";
import { parseKernelTraceView } from "../../kernelTrace";
import type { ControlReceipt, ControlTimelineItem } from "../../api";
import { SourcePill } from "./parts";
import {
  CONTROL_RECEIPT_SECTIONS,
  compact,
  controlReceiptRoute,
  fmtDate,
  moneyFromValue,
  sourceBadgeLabel,
  statusLabel,
  type ControlReceiptSection,
} from "./controlData";

/**
 * Mandate C: the runtime receipt detail — previously a full-page block reached
 * by a route — opens as an in-place right-side DetailSheet layered over the
 * runtime surface. Route-driven data flow is preserved: section switching still
 * navigates via controlReceiptRoute, and onClose returns to the timeline route.
 *
 * Deletion-first merge: the forked receipt body (payload / files / raw-JSON
 * sections) is gone. Everything except the ledger-event header and the kernel
 * trace now renders through the shared RunDetailsPanel body via
 * controlReceiptDetails(), the same generic body used by agent / dag / llm /
 * trial run sheets.
 */
export function ControlReceiptDetailView({
  receiptPath,
  activeSection,
  trigger,
  receipt,
  busy,
  error,
  onClose,
}: {
  receiptPath: string;
  activeSection: ControlReceiptSection;
  trigger: ControlTimelineItem | null;
  receipt: ControlReceipt | null;
  busy: boolean;
  error: string | null;
  onClose: () => void;
}) {
  const title = receipt ? receipt.subject : trigger ? trigger.title : "Control receipt";
  const activeReceiptSection =
    CONTROL_RECEIPT_SECTIONS.find((section) => section.id === activeSection) ||
    CONTROL_RECEIPT_SECTIONS[0];
  const details = receipt ? controlReceiptDetails(receipt, trigger) : null;

  return (
    <DetailSheet
      open
      onClose={onClose}
      size="xl"
      closeLabel="Back to timeline"
      title={<span className="break-words">{title}</span>}
      description={
        <span className="break-all font-mono text-[11px] text-ink-faint">
          {receiptPath}
        </span>
      }
      footer={
        <ToolbarLink href="/runtime/timeline" className="shrink-0">
          Back to timeline
        </ToolbarLink>
      }
    >
      <div className="space-y-5" aria-busy={busy}>
        {busy && <LoadingState label="Loading receipt..." />}
        {error && <InlineAlert tone="red">{error}</InlineAlert>}

        {receipt && details && (
          <SurfacePanel as="section" className="bg-runtime-bg">
            <div className="border-b border-runtime-line-soft/60 px-4 py-3">
              <SegmentedControl
                role="tablist"
                aria-label={`${receipt.receipt_id} receipt sections`}
                className="flex flex-wrap gap-1 bg-runtime-panel/60"
              >
                {CONTROL_RECEIPT_SECTIONS.map((section) => (
                  <TabLink
                    key={section.id}
                    href={controlReceiptRoute(receiptPath, section.id)}
                    selected={activeSection === section.id}
                  >
                    {section.label}
                  </TabLink>
                ))}
              </SegmentedControl>
              <div className="mt-2 text-xs leading-relaxed text-ink-muted">
                {activeReceiptSection.description}
              </div>
            </div>

            <div className="min-w-0 p-4">
              {activeSection === "summary" && (
                <div className="space-y-5">
                  {trigger && <LedgerEventCard trigger={trigger} />}
                  <ReceiptCostHeader receipt={receipt} />
                  <RunDetailsPanel details={details} view="overview" />
                </div>
              )}
              {activeSection === "trace" && <ReceiptTraceSection receipt={receipt} />}
              {activeSection === "payload" && (
                <div className="space-y-4">
                  <RunDetailsPanel details={details} view="payload" />
                  <RunDetailsPanel details={details} view="timeline" />
                </div>
              )}
              {activeSection === "files" && (
                <RunDetailsPanel details={details} view="files" />
              )}
              {activeSection === "metadata" && (
                <RunDetailsPanel details={details} view="payload" />
              )}
            </div>
          </SurfacePanel>
        )}
      </div>
    </DetailSheet>
  );
}

function LedgerEventCard({ trigger }: { trigger: ControlTimelineItem }) {
  return (
    <SurfacePanel as="section" className="bg-runtime-panel/40 p-4" aria-label="Ledger event">
      <div className="flex flex-wrap items-center gap-2">
        <SourcePill source={trigger.source} />
        <StateBadge status={trigger.status} label={statusLabel(trigger.status)} />
        <time className="text-xs text-ink-faint" dateTime={trigger.created_at}>
          {fmtDate(trigger.created_at)}
        </time>
      </div>
      <h2 className="mt-2 break-words text-sm font-semibold text-ink">
        {trigger.title}
      </h2>
      {trigger.summary && (
        <p className="mt-1 text-sm leading-relaxed text-ink-muted">
          {trigger.summary}
        </p>
      )}
    </SurfacePanel>
  );
}

function ReceiptCostHeader({ receipt }: { receipt: ControlReceipt }) {
  return (
    <SurfacePanel as="section" className="bg-runtime-panel/40 p-4" aria-label="Audit packet">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <div className="text-xs uppercase text-ink-muted">Audit packet</div>
          <h2 className="mt-1 text-sm font-semibold text-ink">{receipt.receipt_id}</h2>
        </div>
        <StateBadge status={receipt.status} label={statusLabel(receipt.status)} />
      </div>
      <div className="mt-4 grid grid-cols-2 gap-2 sm:grid-cols-4">
        <SummaryMetric label="Source" value={sourceBadgeLabel(receipt.source)} />
        <SummaryMetric label="Cost" value={moneyFromValue(receipt.costs.cost_usd)} />
        <SummaryMetric label="Tokens" value={compact(Number(receipt.costs.total_tokens || 0))} />
        <SummaryMetric label="Files" value={receipt.file_ops.length} />
      </div>
    </SurfacePanel>
  );
}

function ReceiptTraceSection({ receipt }: { receipt: ControlReceipt }) {
  const trace = parseKernelTraceView({
    trace_summary: receipt.metadata.trace_summary,
    events: receipt.events,
  });
  if (receipt.source !== "protocol_simulation" || !trace) {
    return (
      <EmptyState
        title="No kernel trace"
        description="Trace diagnostics are available for protocol simulation receipts."
        size="compact"
      />
    );
  }
  return <KernelTraceCard trace={trace} />;
}
