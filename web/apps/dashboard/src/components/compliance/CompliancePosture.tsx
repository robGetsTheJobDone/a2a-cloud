import { type ComplianceStatus, type Organization } from "../../api";
import {
  SegmentedControl,
  SummaryMetric,
  SummaryStrip,
  SurfacePanel,
  TabLink,
} from "../DashboardChrome";
import { StatusBadge } from "../StatusPillAdapters";
import { COMPLIANCE_VIEWS, type ComplianceViewId } from "../../navigation";
import {
  compliancePosture,
  complianceRoute,
  formatComplianceDay,
  totalRecordCount,
} from "./shared";

/**
 * CompliancePosture — EU AI Act readiness header for the selected org:
 * enforcement countdown, retention badge, record counts, and agent
 * classification coverage, plus the view tabs. Mirrors OrganizationPosture.
 */
export function CompliancePosture({
  org,
  status,
  activeView,
}: {
  org: Organization;
  status: ComplianceStatus;
  activeView: ComplianceViewId;
}) {
  const posture = compliancePosture(status);
  const unclassified = status.agents_total - status.agents_classified;
  const recordTotal = totalRecordCount(status);

  return (
    <SurfacePanel as="section" className="overflow-hidden bg-runtime-bg/80">
      <div className="flex flex-col gap-4 border-b border-runtime-line-soft/70 p-4 sm:p-5 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <StatusBadge tone={posture.tone} dot={posture.tone === "emerald"}>
              {posture.label}
            </StatusBadge>
            <StatusBadge tone={status.retention_ok ? "emerald" : "red"}>
              {status.retention_ok ? "retention ok" : "retention gap"}
            </StatusBadge>
            {status.policy.legal_hold && (
              <StatusBadge tone="amber">legal hold</StatusBadge>
            )}
            <span className="text-xs text-ink-muted">{posture.detail}</span>
          </div>
          <h2 className="mt-3 truncate text-lg font-semibold text-ink">
            EU AI Act readiness — {org.name}
          </h2>
          <div className="mt-1 text-xs text-ink-muted">
            Enforcement date{" "}
            <span className="font-mono text-ink-soft">
              {formatComplianceDay(status.enforcement_date)}
            </span>
            {" · "}retention window{" "}
            <span className="font-mono text-ink-soft">
              {status.policy.retention_days.toLocaleString()} days
            </span>
            {" · "}oldest record{" "}
            <span className="font-mono text-ink-soft">
              {formatComplianceDay(status.oldest_record_at) === "unknown"
                ? "none"
                : formatComplianceDay(status.oldest_record_at)}
            </span>
          </div>
        </div>
      </div>

      <div className="grid gap-4 p-4 sm:p-5">
        <SummaryStrip aria-label={`${org.name} compliance readiness summary`}>
          <SummaryMetric
            label="days until enforcement"
            value={status.days_until_enforcement.toLocaleString()}
            tone={
              status.days_until_enforcement <= 30
                ? "red"
                : status.days_until_enforcement <= 90
                  ? "amber"
                  : "neutral"
            }
            detail={formatComplianceDay(status.enforcement_date)}
          />
          <SummaryMetric
            label="decision records"
            value={recordTotal.toLocaleString()}
            detail={`${status.record_counts.skill_execution.toLocaleString()} skill · ${status.record_counts.authorization.toLocaleString()} authz · ${status.record_counts.admin_action.toLocaleString()} admin`}
          />
          <SummaryMetric
            label="agents classified"
            value={`${status.agents_classified}/${status.agents_total}`}
            tone={unclassified > 0 ? "amber" : status.agents_total > 0 ? "emerald" : "neutral"}
            detail={
              unclassified > 0
                ? `${unclassified.toLocaleString()} unclassified`
                : "full risk-tier coverage"
            }
          />
          <SummaryMetric
            label="high-risk agents"
            value={status.agents_high_risk.toLocaleString()}
            tone={status.agents_high_risk > 0 ? "amber" : "neutral"}
            detail="high or unacceptable tier"
          />
        </SummaryStrip>
      </div>

      <div className="border-t border-runtime-line-soft/70 px-4 pb-4 sm:px-5 sm:pb-5">
        <SegmentedControl
          role="tablist"
          aria-label={`${org.name} compliance views`}
          className="mt-4 flex gap-1 overflow-x-auto bg-runtime-panel/60"
        >
          {COMPLIANCE_VIEWS.map((view) => (
            <TabLink
              key={view.id}
              href={complianceRoute(org.slug, view.id)}
              selected={activeView === view.id}
              className="whitespace-nowrap"
            >
              {view.label}
            </TabLink>
          ))}
        </SegmentedControl>
      </div>
    </SurfacePanel>
  );
}
