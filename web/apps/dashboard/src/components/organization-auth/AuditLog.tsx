import { useMemo } from "react";
import { useNavigate } from "react-router-dom";
import { type Organization, type OrganizationAuditLog } from "../../api";
import {
  CodeBlock,
  CopyValueRow,
  EmptyState,
  InlineAlert,
  SectionPanel,
  SelectableSurfaceLink,
  SummaryMetric,
  ToolbarButton,
} from "../DashboardChrome";
import { DetailSheet } from "../ListDetailLayout";
import {
  auditTargetLabel,
  formatAuditData,
  formatDate,
  organizationAuditLogRoute,
  organizationRoute,
} from "./shared";

/**
 * AuditLog — recent governance events. The per-entry detail (mandate C) opens
 * as an in-place right-side DetailSheet driven by the route segment.
 */
export function AuditPanel({
  org,
  logs,
  selectedLogId,
}: {
  org: Organization;
  logs: OrganizationAuditLog[];
  selectedLogId: string | null;
}) {
  const navigate = useNavigate();
  const selectedLog = useMemo(
    () =>
      selectedLogId
        ? logs.find((log) => String(log.id) === selectedLogId) ?? null
        : null,
    [logs, selectedLogId],
  );

  const closeSheet = () => navigate(organizationRoute(org.slug, "audit"));

  return (
    <>
      <SectionPanel title="Audit log">
        {logs.length === 0 ? (
          <EmptyState title="No audit entries" />
        ) : (
          <div className="space-y-2">
            {logs.map((log) => (
              <AuditLogRow
                key={log.id}
                org={org}
                log={log}
                selected={String(log.id) === selectedLogId}
              />
            ))}
          </div>
        )}
      </SectionPanel>

      <AuditLogDetail
        open={Boolean(selectedLogId)}
        onClose={closeSheet}
        log={selectedLog}
        selectedLogId={selectedLogId ?? ""}
      />
    </>
  );
}

function AuditLogRow({
  org,
  log,
  selected = false,
}: {
  org: Organization;
  log: OrganizationAuditLog;
  selected?: boolean;
}) {
  return (
    <SelectableSurfaceLink
      href={organizationAuditLogRoute(org.slug, log.id)}
      selected={selected}
      className="p-3 text-xs"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="min-w-0">
          <div className="truncate font-mono text-xs text-ink-soft">{log.action}</div>
          <div className="mt-1 truncate text-sm text-ink">{auditTargetLabel(log)}</div>
        </div>
        <div className="shrink-0 text-xs text-ink-muted">{formatDate(log.created_at)}</div>
      </div>
      <div className="mt-2 flex flex-wrap gap-2 text-xs text-ink-muted">
        <span>{log.actor || "system"}</span>
        <span>{log.target_type}</span>
        <span>{formatAuditData(log.data)}</span>
        <span className="text-ink-soft">{selected ? "Open in detail" : "Open"}</span>
      </div>
    </SelectableSurfaceLink>
  );
}

function AuditLogDetail({
  open,
  onClose,
  log,
  selectedLogId,
}: {
  open: boolean;
  onClose: () => void;
  log: OrganizationAuditLog | null;
  selectedLogId: string;
}) {
  if (!log) {
    return (
      <DetailSheet open={open} onClose={onClose} title={selectedLogId} description="Audit entry">
        <InlineAlert tone="amber" className="text-xs">
          This audit entry is not in the current audit log window.
        </InlineAlert>
        <ToolbarButton onClick={onClose} className="mt-3">
          Back to audit log
        </ToolbarButton>
      </DetailSheet>
    );
  }

  return (
    <DetailSheet
      open={open}
      onClose={onClose}
      title={
        <span className="font-mono text-base text-ink [overflow-wrap:anywhere]">
          {log.action}
        </span>
      }
      description={auditTargetLabel(log)}
      size="lg"
    >
      <div className="grid gap-4">
        <div className="grid gap-2 md:grid-cols-2">
          <SummaryMetric label="entry id" value={log.id.toString()} size="compact" />
          <SummaryMetric label="action" value={log.action} size="compact" />
          <SummaryMetric label="created" value={formatDate(log.created_at)} size="compact" mono={false} />
          <SummaryMetric label="actor" value={log.actor || "system"} size="compact" />
        </div>

        <SectionPanel title="Target">
          <div className="grid gap-2">
            <CopyValueRow label="type" value={log.target_type} />
            <CopyValueRow label="id" value={log.target_id || "none"} />
            <CopyValueRow label="email" value={log.target_email || "none"} />
            <CopyValueRow
              label="actor user id"
              value={log.actor_user_id?.toString() || "system"}
            />
          </div>
        </SectionPanel>

        <SectionPanel title="Metadata" description="Raw audit metadata for the governance event.">
          <CodeBlock className="max-h-[420px] text-xs">
            {JSON.stringify(log.data, null, 2)}
          </CodeBlock>
        </SectionPanel>
      </div>
    </DetailSheet>
  );
}
