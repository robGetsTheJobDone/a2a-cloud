import { useCallback, useEffect, useState, type FormEvent } from "react";
import {
  getComplianceDecisionRecord,
  listComplianceDecisionRecords,
  type ComplianceDecisionRecord,
  type ComplianceDecisionRecordDetail,
  type ComplianceDecisionRecordKind,
} from "../../api";
import {
  CodeBlock,
  CopyValueRow,
  DataTable,
  EmptyState,
  FilterBar,
  FormField,
  InlineAlert,
  LoadingState,
  SectionPanel,
  SelectInput,
  SummaryMetric,
  TextInput,
  ToolbarButton,
} from "../DashboardChrome";
import { StatusBadge } from "../StatusPillAdapters";
import { DetailSheet } from "../ListDetailLayout";
import {
  DECISION_RECORD_KINDS,
  dateInputToIso,
  decisionRecordKindLabel,
  formatComplianceDate,
  outcomeTone,
  signatureStatusLabel,
  signatureStatusTone,
} from "./shared";

const RECORDS_LIMIT = 200;

type RecordFilterDraft = {
  kind: ComplianceDecisionRecordKind | "";
  agent: string;
  outcome: string;
  fromDay: string;
  toDay: string;
};

const EMPTY_FILTERS: RecordFilterDraft = {
  kind: "",
  agent: "",
  outcome: "",
  fromDay: "",
  toDay: "",
};

const RECORD_GRID_CLASS =
  "grid min-w-[860px] grid-cols-[130px_minmax(160px,1.2fr)_minmax(200px,1.6fr)_110px_110px_150px] gap-3 px-3";

/**
 * DecisionRecordsPanel — filterable decision-record ledger. Row click opens an
 * in-place right-side DetailSheet (mandate C) with the full signed payload and
 * signature verification badge.
 */
export function DecisionRecordsPanel({ orgSlug }: { orgSlug: string }) {
  const [draft, setDraft] = useState<RecordFilterDraft>(EMPTY_FILTERS);
  const [applied, setApplied] = useState<RecordFilterDraft>(EMPTY_FILTERS);
  const [records, setRecords] = useState<ComplianceDecisionRecord[] | null>(null);
  const [listErr, setListErr] = useState<string | null>(null);
  const [selected, setSelected] = useState<ComplianceDecisionRecord | null>(null);

  useEffect(() => {
    let cancelled = false;
    setListErr(null);
    setRecords(null);
    listComplianceDecisionRecords(orgSlug, {
      kind: applied.kind || undefined,
      agent: applied.agent.trim() || undefined,
      outcome: applied.outcome.trim() || undefined,
      from_at: dateInputToIso(applied.fromDay),
      to_at: dateInputToIso(applied.toDay, true),
      limit: RECORDS_LIMIT,
    })
      .then((rows) => {
        if (!cancelled) setRecords(rows);
      })
      .catch((ex) => {
        if (!cancelled) {
          setListErr(ex instanceof Error ? ex.message : String(ex));
          setRecords([]);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [applied, orgSlug]);

  function handleApply(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setApplied(draft);
  }

  function handleReset() {
    setDraft(EMPTY_FILTERS);
    setApplied(EMPTY_FILTERS);
  }

  return (
    <SectionPanel
      title="Decision records"
      description="Tamper-evident record of skill executions, authorization decisions, and admin actions."
    >
      <div className="grid gap-4">
        <form onSubmit={handleApply}>
          <FilterBar
            actions={
              <div className="flex gap-2">
                <ToolbarButton type="submit" variant="primary" size="sm">
                  Apply filters
                </ToolbarButton>
                <ToolbarButton type="button" size="sm" onClick={handleReset}>
                  Reset
                </ToolbarButton>
              </div>
            }
          >
            <FormField label="Kind" className="w-40">
              <SelectInput
                compact
                value={draft.kind}
                onChange={(event) =>
                  setDraft((current) => ({
                    ...current,
                    kind: event.target.value as ComplianceDecisionRecordKind | "",
                  }))
                }
              >
                <option value="">All kinds</option>
                {DECISION_RECORD_KINDS.map((kind) => (
                  <option key={kind.id} value={kind.id}>
                    {kind.label}
                  </option>
                ))}
              </SelectInput>
            </FormField>
            <FormField label="Agent" className="w-44">
              <TextInput
                compact
                value={draft.agent}
                placeholder="agent name"
                onChange={(event) =>
                  setDraft((current) => ({ ...current, agent: event.target.value }))
                }
              />
            </FormField>
            <FormField label="Outcome" className="w-36">
              <TextInput
                compact
                value={draft.outcome}
                placeholder="e.g. allowed"
                onChange={(event) =>
                  setDraft((current) => ({ ...current, outcome: event.target.value }))
                }
              />
            </FormField>
            <FormField label="From" className="w-40">
              <TextInput
                compact
                type="date"
                value={draft.fromDay}
                onChange={(event) =>
                  setDraft((current) => ({ ...current, fromDay: event.target.value }))
                }
              />
            </FormField>
            <FormField label="To" className="w-40">
              <TextInput
                compact
                type="date"
                value={draft.toDay}
                onChange={(event) =>
                  setDraft((current) => ({ ...current, toDay: event.target.value }))
                }
              />
            </FormField>
          </FilterBar>
        </form>

        {listErr && <InlineAlert tone="red">{listErr}</InlineAlert>}

        {records === null ? (
          <LoadingState label="Loading decision records..." />
        ) : records.length === 0 ? (
          <EmptyState
            title="No decision records"
            description="No records match the current filters. Decision records appear as agents execute skills and admins act."
          />
        ) : (
          <DataTable>
            <div
              className={`${RECORD_GRID_CLASS} border-b border-runtime-line-soft/70 bg-runtime-bg py-2 text-[10px] uppercase text-ink-faint`}
            >
              <div>Kind</div>
              <div>Agent</div>
              <div>Action</div>
              <div>Outcome</div>
              <div>Verifiable</div>
              <div>Recorded</div>
            </div>
            <div className="divide-y divide-runtime-line-soft">
              {records.map((record) => (
                <button
                  key={`${record.kind}:${record.record_id}`}
                  type="button"
                  onClick={() => setSelected(record)}
                  className={`${RECORD_GRID_CLASS} w-full py-3 text-left text-xs transition hover:bg-runtime-panel/40 ${
                    selected?.record_id === record.record_id &&
                    selected?.kind === record.kind
                      ? "bg-signal-authority/12"
                      : ""
                  }`}
                >
                  <div className="truncate text-ink-dim">
                    {decisionRecordKindLabel(record.kind)}
                  </div>
                  <div className="min-w-0">
                    <div className="truncate font-mono text-ink-soft">
                      {record.agent_name ?? "—"}
                    </div>
                    <div className="mt-1 truncate text-ink-faint">{record.actor}</div>
                  </div>
                  <div className="truncate font-mono text-ink-dim">{record.action}</div>
                  <div>
                    <StatusBadge tone={outcomeTone(record.outcome)}>
                      {record.outcome}
                    </StatusBadge>
                  </div>
                  <div>
                    <StatusBadge tone={record.verifiable ? "emerald" : "neutral"}>
                      {record.verifiable ? "signed" : "unsigned"}
                    </StatusBadge>
                  </div>
                  <div className="truncate text-ink-muted">
                    {formatComplianceDate(record.recorded_at)}
                  </div>
                </button>
              ))}
            </div>
          </DataTable>
        )}
      </div>

      <DecisionRecordDetail
        orgSlug={orgSlug}
        record={selected}
        onClose={() => setSelected(null)}
      />
    </SectionPanel>
  );
}

function DecisionRecordDetail({
  orgSlug,
  record,
  onClose,
}: {
  orgSlug: string;
  record: ComplianceDecisionRecord | null;
  onClose: () => void;
}) {
  const [detail, setDetail] = useState<ComplianceDecisionRecordDetail | null>(null);
  const [detailErr, setDetailErr] = useState<string | null>(null);

  const loadDetail = useCallback(() => {
    if (!record) return;
    setDetail(null);
    setDetailErr(null);
    getComplianceDecisionRecord(orgSlug, record.kind, record.record_id)
      .then(setDetail)
      .catch((ex) => setDetailErr(ex instanceof Error ? ex.message : String(ex)));
  }, [orgSlug, record]);

  useEffect(() => {
    loadDetail();
  }, [loadDetail]);

  return (
    <DetailSheet
      open={Boolean(record)}
      onClose={onClose}
      title={
        <span className="font-mono text-base text-ink [overflow-wrap:anywhere]">
          {record?.action ?? "Decision record"}
        </span>
      }
      description={record ? decisionRecordKindLabel(record.kind) : undefined}
      size="lg"
    >
      {detailErr && (
        <div className="grid gap-3">
          <InlineAlert tone="red">{detailErr}</InlineAlert>
          <ToolbarButton onClick={loadDetail} className="justify-self-start">
            Retry
          </ToolbarButton>
        </div>
      )}
      {!detailErr && !detail && <LoadingState label="Loading record..." />}
      {detail && (
        <div className="grid gap-4">
          <div className="flex flex-wrap items-center gap-2">
            <StatusBadge
              tone={signatureStatusTone(detail.signature_status)}
              dot={detail.signature_status === "valid"}
            >
              {signatureStatusLabel(detail.signature_status)}
            </StatusBadge>
            <StatusBadge tone={outcomeTone(detail.outcome)}>{detail.outcome}</StatusBadge>
          </div>

          <div className="grid gap-2 md:grid-cols-2">
            <SummaryMetric label="record id" value={detail.record_id} size="compact" />
            <SummaryMetric
              label="kind"
              value={decisionRecordKindLabel(detail.kind)}
              size="compact"
              mono={false}
            />
            <SummaryMetric
              label="occurred"
              value={formatComplianceDate(detail.occurred_at)}
              size="compact"
              mono={false}
            />
            <SummaryMetric
              label="recorded"
              value={formatComplianceDate(detail.recorded_at)}
              size="compact"
              mono={false}
            />
          </div>

          <SectionPanel title="Actor">
            <div className="grid gap-2">
              <CopyValueRow label="actor" value={detail.actor} />
              <CopyValueRow label="agent" value={detail.agent_name ?? "none"} />
              <CopyValueRow label="action" value={detail.action} />
            </div>
          </SectionPanel>

          {detail.signed_token && (
            <SectionPanel
              title="Signed token"
              description="The signed receipt token backing this record's verification."
            >
              <CodeBlock className="max-h-[160px] text-xs">
                {detail.signed_token}
              </CodeBlock>
            </SectionPanel>
          )}

          <SectionPanel
            title="Payload"
            description="Full decision-record payload as stored for evidence export."
          >
            <CodeBlock className="max-h-[420px] text-xs">
              {JSON.stringify(detail.payload, null, 2)}
            </CodeBlock>
          </SectionPanel>
        </div>
      )}
    </DetailSheet>
  );
}
