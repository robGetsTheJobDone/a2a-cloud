import {
  EmptyState,
  FormField,
  InlineAlert,
  SegmentedControl,
  SummaryMetric,
  SurfacePanel,
  TabLink,
  TextArea,
  ToggleField,
  ToolbarButton,
  ToolbarLink,
} from "../DashboardChrome";
import { StatusBadge } from "../StatusPillAdapters";
import type { ControlPolicy, ControlSummary, ControlTimelineItem } from "../../api";
import { NumberField } from "./parts";
import {
  CONTROL_POLICY_SECTIONS,
  controlPolicyRoute,
  money,
  type ControlPolicySection,
} from "./controlData";
import { PolicySnapshot, RuntimeStatePanel } from "./ControlPanels";

export function ControlPolicyView({
  activeSection,
  summary,
  liveDagRuns,
  policy,
  dirty,
  busy,
  error,
  onSave,
  onPolicyChange,
}: {
  activeSection: ControlPolicySection;
  summary: ControlSummary;
  liveDagRuns: ControlTimelineItem[];
  policy: ControlPolicy | null;
  dirty: boolean;
  busy: boolean;
  error: string | null;
  onSave: () => void;
  onPolicyChange: (policy: ControlPolicy) => void;
}) {
  const activePolicySection =
    CONTROL_POLICY_SECTIONS.find((section) => section.id === activeSection) ||
    CONTROL_POLICY_SECTIONS[0];

  return (
    <>
      {policy ? (
        <div>
          <ControlPolicyNav activeSection={activeSection} />
          <p className="mt-2 text-sm leading-relaxed text-ink-muted">
            {activePolicySection.description}
          </p>
        </div>
      ) : null}

      {policy ? (
        <div className="grid gap-5 xl:grid-cols-[minmax(280px,0.8fr)_minmax(0,1.2fr)]">
          <div className="space-y-5">
            <RuntimeStatePanel
              summary={summary}
              liveDagRuns={liveDagRuns}
            />
            <PolicySnapshot
              policy={policy}
              summary={summary}
              dirty={dirty}
            />
          </div>
          {activeSection === "overview" ? (
            <PolicyOverviewPanel
              policy={policy}
              summary={summary}
              dirty={dirty}
              busy={busy}
              error={error}
              onSave={onSave}
            />
          ) : (
            <ControlPolicyEditor
              activeSection={activeSection}
              policy={policy}
              dirty={dirty}
              busy={busy}
              error={error}
              onSave={onSave}
              onPolicyChange={onPolicyChange}
            />
          )}
        </div>
      ) : (
        <div className="mt-5">
          <EmptyState
            title="Policy unavailable"
            description="Runtime state loaded without editable policy controls."
          />
        </div>
      )}
    </>
  );
}

function PolicyOverviewPanel({
  policy,
  summary,
  dirty,
  busy,
  error,
  onSave,
}: {
  policy: ControlPolicy;
  summary: ControlSummary;
  dirty: boolean;
  busy: boolean;
  error: string | null;
  onSave: () => void;
}) {
  const gates = [
    {
      label: "File write approval",
      enabled: policy.require_approval_for_file_writes,
    },
    {
      label: "External network block",
      enabled: policy.deny_external_network,
    },
    {
      label: "Approved-agent routing",
      enabled: policy.only_approved_agents,
    },
    {
      label: "PII-safe mode",
      enabled: policy.pii_safe_mode,
    },
  ];
  const previewAgents = policy.approved_agents.slice(0, 8);
  const remainingAgents = Math.max(0, policy.approved_agents.length - previewAgents.length);

  return (
    <SurfacePanel
      as="section"
      className="mt-5 space-y-5 bg-runtime-bg/70 p-4"
      aria-labelledby="control-policy-overview-heading"
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 id="control-policy-overview-heading" className="text-sm font-semibold text-ink">
            Policy overview
          </h2>
          <p className="mt-1 text-sm leading-relaxed text-ink-muted">
            A read-only map of the active runtime guardrails. Use the focused sections for edits.
          </p>
        </div>
        <StatusBadge tone={dirty ? "amber" : "emerald"} dot>
          {dirty ? "draft" : "active"}
        </StatusBadge>
      </div>

      <div className="grid grid-cols-2 gap-2 lg:grid-cols-4">
        <SummaryMetric label="Monthly cap" value={money(policy.monthly_budget_cents)} />
        <SummaryMetric label="Per-run cap" value={money(policy.run_budget_cents)} />
        <SummaryMetric label="Max handoffs" value={policy.max_agent_calls_per_run} />
        <SummaryMetric label="Month spend" value={money(summary.monthly_spend_cents)} />
      </div>

      <div>
        <div className="text-xs uppercase text-ink-muted">
          Gates
        </div>
        <div className="mt-2 flex flex-wrap gap-1.5">
          {gates.map((gate) => (
            <span
              key={gate.label}
              className="rounded-md border border-runtime-line-soft/60 bg-runtime-panel/70 px-2 py-1 text-xs text-ink-dim"
            >
              {gate.label}:{" "}
              <span className={gate.enabled ? "text-signal-live" : "text-ink-faint"}>
                {gate.enabled ? "on" : "off"}
              </span>
            </span>
          ))}
        </div>
      </div>

      <div>
        <div className="text-xs uppercase text-ink-muted">
          Approved agents
        </div>
        {previewAgents.length > 0 ? (
          <div className="mt-2 flex flex-wrap gap-1.5">
            {previewAgents.map((agent) => (
              <span
                key={agent}
                className="max-w-full rounded-md border border-runtime-line-soft/60 bg-runtime-panel/70 px-2 py-1 font-mono text-xs text-ink-dim"
              >
                {agent}
              </span>
            ))}
            {remainingAgents > 0 && (
              <span className="rounded-md border border-runtime-line-soft/60 bg-runtime-panel/70 px-2 py-1 text-xs text-ink-muted">
                +{remainingAgents} more
              </span>
            )}
          </div>
        ) : (
          <p className="mt-2 text-sm leading-relaxed text-ink-muted">
            No approved agents are configured.
          </p>
        )}
      </div>

      {policy.only_approved_agents && policy.approved_agents.length === 0 && (
        <InlineAlert tone="amber">
          Allowlist enforcement is active with no approved agents.
        </InlineAlert>
      )}

      <div className="grid gap-2 sm:grid-cols-3">
        <ToolbarLink href={controlPolicyRoute("budgets")}>
          Edit budgets
        </ToolbarLink>
        <ToolbarLink href={controlPolicyRoute("gates")}>
          Edit gates
        </ToolbarLink>
        <ToolbarLink href={controlPolicyRoute("allowlist")}>
          Edit allowlist
        </ToolbarLink>
      </div>

      {error && (
        <div role="alert">
          <InlineAlert tone="red">
            {error}
          </InlineAlert>
        </div>
      )}
      <ToolbarButton
        type="button"
        onClick={onSave}
        disabled={busy || !dirty}
        variant="primary"
        className="w-full sm:w-auto"
        aria-busy={busy}
      >
        {busy ? "Saving..." : dirty ? "Save controls" : "Controls saved"}
      </ToolbarButton>
    </SurfacePanel>
  );
}

function ControlPolicyNav({ activeSection }: { activeSection: ControlPolicySection }) {
  return (
    <SegmentedControl
      role="tablist"
      aria-label="Runtime policy sections"
      className="flex flex-wrap gap-1"
    >
      {CONTROL_POLICY_SECTIONS.map((section) => (
        <TabLink
          key={section.id}
          href={controlPolicyRoute(section.id)}
          selected={section.id === activeSection}
        >
          {section.label}
        </TabLink>
      ))}
    </SegmentedControl>
  );
}

function ControlPolicyEditor({
  activeSection,
  policy,
  dirty,
  busy,
  error,
  onSave,
  onPolicyChange,
}: {
  activeSection: Exclude<ControlPolicySection, "overview">;
  policy: ControlPolicy;
  dirty: boolean;
  busy: boolean;
  error: string | null;
  onSave: () => void;
  onPolicyChange: (policy: ControlPolicy) => void;
}) {
  const activePolicySection =
    CONTROL_POLICY_SECTIONS.find((section) => section.id === activeSection) ||
    CONTROL_POLICY_SECTIONS[1];

  return (
    <SurfacePanel
      as="section"
      className="mt-5 space-y-5 bg-runtime-bg/70 p-4"
      aria-labelledby="control-policy-heading"
    >
      <div>
        <h2 id="control-policy-heading" className="text-sm font-semibold text-ink">
          {activePolicySection.label}
        </h2>
        <p className="mt-1 text-sm leading-relaxed text-ink-muted">
          {activePolicySection.description}
        </p>
      </div>

      {activeSection === "budgets" && (
        <PolicyBudgetFields policy={policy} onPolicyChange={onPolicyChange} />
      )}
      {activeSection === "gates" && (
        <PolicyGateFields policy={policy} onPolicyChange={onPolicyChange} />
      )}
      {activeSection === "allowlist" && (
        <PolicyAllowlistField policy={policy} onPolicyChange={onPolicyChange} />
      )}

      {error && (
        <div role="alert">
          <InlineAlert tone="red">
            {error}
          </InlineAlert>
        </div>
      )}
      <ToolbarButton
        type="button"
        onClick={onSave}
        disabled={busy || !dirty}
        variant="primary"
        className="w-full sm:w-auto"
        aria-busy={busy}
      >
        {busy ? "Saving..." : dirty ? "Save controls" : "Controls saved"}
      </ToolbarButton>
    </SurfacePanel>
  );
}

function PolicyBudgetFields({
  policy,
  onPolicyChange,
}: {
  policy: ControlPolicy;
  onPolicyChange: (policy: ControlPolicy) => void;
}) {
  return (
    <div>
      <div className="text-xs uppercase text-ink-muted">
        Budget caps
      </div>
      <p className="mt-1 text-sm leading-relaxed text-ink-muted">
        Dollar values are stored as cents and enforced by the control plane.
      </p>
      <div className="mt-3 grid gap-2 sm:grid-cols-3">
        <NumberField
          label="Monthly cap"
          value={policy.monthly_budget_cents}
          onChange={(value) => onPolicyChange({ ...policy, monthly_budget_cents: value })}
        />
        <NumberField
          label="Per-run cap"
          value={policy.run_budget_cents}
          onChange={(value) => onPolicyChange({ ...policy, run_budget_cents: value })}
        />
        <NumberField
          label="Max handoffs"
          value={policy.max_agent_calls_per_run}
          onChange={(value) => onPolicyChange({ ...policy, max_agent_calls_per_run: value })}
          plain
        />
      </div>
    </div>
  );
}

function PolicyGateFields({
  policy,
  onPolicyChange,
}: {
  policy: ControlPolicy;
  onPolicyChange: (policy: ControlPolicy) => void;
}) {
  return (
    <div className="grid gap-2 sm:grid-cols-2" aria-label="Control gates">
      <ToggleField
        label="Approve file writes"
        checked={policy.require_approval_for_file_writes}
        onCheckedChange={(value) => onPolicyChange({ ...policy, require_approval_for_file_writes: value })}
      />
      <ToggleField
        label="Block external network"
        checked={policy.deny_external_network}
        onCheckedChange={(value) => onPolicyChange({ ...policy, deny_external_network: value })}
      />
      <ToggleField
        label="Require approved agents"
        checked={policy.only_approved_agents}
        onCheckedChange={(value) => onPolicyChange({ ...policy, only_approved_agents: value })}
      />
      <ToggleField
        label="PII-safe mode"
        checked={policy.pii_safe_mode}
        onCheckedChange={(value) => onPolicyChange({ ...policy, pii_safe_mode: value })}
      />
    </div>
  );
}

function PolicyAllowlistField({
  policy,
  onPolicyChange,
}: {
  policy: ControlPolicy;
  onPolicyChange: (policy: ControlPolicy) => void;
}) {
  return (
    <>
      <FormField label="Agent allowlist">
        <TextArea
          value={policy.approved_agents.join("\n")}
          onChange={(event) =>
            onPolicyChange({
              ...policy,
              approved_agents: event.target.value
                .split(/\s+/)
                .map((item) => item.trim())
                .filter(Boolean),
            })
          }
          rows={8}
          mono
          compact
          className="resize-none"
        />
      </FormField>
      {policy.only_approved_agents && policy.approved_agents.length === 0 && (
        <InlineAlert tone="amber">
          Allowlist enforcement is active with no approved agents.
        </InlineAlert>
      )}
    </>
  );
}
