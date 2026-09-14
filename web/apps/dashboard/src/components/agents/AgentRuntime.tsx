import { useEffect, useState } from "react";
import {
  listAgentSelfHealing,
  type AgentSelfHealingHistory,
  type AgentSelfHealingRun,
  type AgentTemplateUpdateResult,
  type MyAgentListing,
} from "../../api";
import {
  DefinitionRow,
  InlineAlert,
  SurfacePanel,
  TextInput,
  ToolbarButton,
  ToolbarLink,
} from "../DashboardChrome";
import {
  type TemplateLineageView,
  templateUpdateButtonLabel,
  templateUpdatePolicy,
} from "./agentTypes";

export function AgentRuntimePanel({
  agent,
  runtime,
  upgrade,
  templateLineage,
  codeEditorEnabled,
  cardRefreshBusy,
  openApiRefreshBusy,
  codeEditorBusy,
  agentDeployActive,
  openApiSourceUrl,
  openApiRefreshErr,
  cardRefreshErr,
  codeEditorErr,
  deleteOpen,
  deleteConfirm,
  deleteBusy,
  deleteErr,
  templateUpdateBusy,
  templateUpdateResult,
  templateUpdateErr,
  onRefreshCard,
  onToggleCodeEditor,
  onRefreshOpenApi,
  onDeleteToggle,
  onDeleteConfirmChange,
  onDelete,
  onRequestTemplateUpdate,
}: {
  agent: MyAgentListing;
  runtime: MyAgentListing["card"]["runtime"] | undefined;
  upgrade: MyAgentListing["runtime_upgrade"] | undefined;
  templateLineage: MyAgentListing["card"]["template_lineage"] | null | undefined;
  codeEditorEnabled: boolean;
  cardRefreshBusy: boolean;
  openApiRefreshBusy: boolean;
  codeEditorBusy: boolean;
  agentDeployActive: boolean;
  openApiSourceUrl: string | null;
  openApiRefreshErr: string | null;
  cardRefreshErr: string | null;
  codeEditorErr: string | null;
  deleteOpen: boolean;
  deleteConfirm: string;
  deleteBusy: boolean;
  deleteErr: string | null;
  templateUpdateBusy: boolean;
  templateUpdateResult: AgentTemplateUpdateResult | null;
  templateUpdateErr: string | null;
  onRefreshCard: () => void;
  onToggleCodeEditor: () => void;
  onRefreshOpenApi: () => void;
  onDeleteToggle: () => void;
  onDeleteConfirmChange: (value: string) => void;
  onDelete: () => void;
  onRequestTemplateUpdate: () => void;
}) {
  const [healingHistory, setHealingHistory] =
    useState<AgentSelfHealingHistory | null>(null);
  const [healingBusy, setHealingBusy] = useState(true);
  const [healingError, setHealingError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setHealingBusy(true);
    setHealingError(null);
    const load = () => {
      listAgentSelfHealing(agent.name)
        .then((value) => {
          if (!cancelled) {
            setHealingHistory(value);
            setHealingError(null);
          }
        })
        .catch((error: unknown) => {
          if (!cancelled) {
            setHealingError(error instanceof Error ? error.message : String(error));
          }
        })
        .finally(() => {
          if (!cancelled) setHealingBusy(false);
        });
    };
    load();
    const timer = window.setInterval(load, 5_000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [agent.name, agent.latest_deployment?.updated_at]);

  return (
    <div className="mt-4 border-t border-runtime-line-soft/60 pt-4">
      <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
        <div>
          <div className="text-[10px] uppercase text-ink-faint">
            Runtime maintenance
          </div>
          <div className="mt-1 text-sm text-ink-soft">
            Live card, code editor, OpenAPI regeneration, template lineage, and delete controls.
          </div>
        </div>
        <div className="flex flex-wrap gap-2">
          <ToolbarButton
            onClick={onRefreshCard}
            disabled={cardRefreshBusy || agentDeployActive}
            title="Re-read the agent's live card (wakes the agent)"
            size="sm"
          >
            {cardRefreshBusy ? "refreshing..." : "refresh card"}
          </ToolbarButton>
          <ToolbarButton
            onClick={onToggleCodeEditor}
            disabled={codeEditorBusy}
            size="sm"
          >
            {codeEditorBusy
              ? "editor..."
              : codeEditorEnabled
                ? "disable editor"
                : "enable editor"}
          </ToolbarButton>
          {openApiSourceUrl && (
            <ToolbarButton
              onClick={onRefreshOpenApi}
              disabled={agentDeployActive || openApiRefreshBusy}
              size="sm"
            >
              {openApiRefreshBusy ? "refreshing..." : "refresh OpenAPI"}
            </ToolbarButton>
          )}
          <ToolbarButton
            onClick={onDeleteToggle}
            disabled={deleteBusy}
            variant="danger"
            size="sm"
          >
            delete agent
          </ToolbarButton>
        </div>
      </div>

      {openApiRefreshErr && (
        <InlineAlert tone="red" role="alert" className="mt-4 text-xs">
          {openApiRefreshErr}
        </InlineAlert>
      )}

      {cardRefreshErr && (
        <InlineAlert tone="red" role="alert" className="mt-4 text-xs">
          {cardRefreshErr}
        </InlineAlert>
      )}

      {codeEditorErr && (
        <InlineAlert tone="red" role="alert" className="mt-4 text-xs">
          {codeEditorErr}
        </InlineAlert>
      )}

      <SurfacePanel as="dl" className="mt-4 grid gap-2 bg-runtime-panel/40 p-3 text-xs md:grid-cols-2">
        <DefinitionRow label="image" value={agent.image} mono />
        <DefinitionRow
          label="llm"
          value={runtime?.llm_provisioning || "platform"}
        />
        {runtime?.account_access?.required ? (
          <DefinitionRow
            label="access"
            value={`${runtime.account_access.platform_skill_calls} funded calls per account, then BYOK`}
          />
        ) : null}
        <DefinitionRow
          label="sdk"
          value={
            upgrade
              ? `${upgrade.current_version || "unknown"} -> ${upgrade.latest_version}`
              : "unknown"
          }
        />
        <DefinitionRow
          label="code editor"
          value={codeEditorEnabled ? "enabled" : "disabled"}
        />
        <DefinitionRow
          label="self-healing"
          value={agent.self_healing?.enabled ? "enabled by a2a.yaml" : "disabled"}
        />
      </SurfacePanel>

      <SelfHealingPanel
        policy={agent.self_healing}
        history={healingHistory}
        busy={healingBusy}
        error={healingError}
      />

      {templateLineage && (
        <TemplateLineagePanel
          lineage={templateLineage}
          busy={templateUpdateBusy}
          blocked={agentDeployActive}
          result={templateUpdateResult}
          error={templateUpdateErr}
          onRequest={onRequestTemplateUpdate}
        />
      )}

      {deleteOpen && (
        <InlineAlert tone="red" className="mt-4">
          <div className="flex flex-col gap-3 lg:flex-row lg:items-end lg:justify-between">
            <div className="min-w-0">
              <div className="text-xs font-semibold">
                Delete {agent.name}
              </div>
              <div className="mt-1 text-xs leading-relaxed text-signal-danger/70">
                Removes the control-plane record, Argo app, repo credential,
                Kubernetes resources, and Gitea source repo. If cleanup fails,
                this row is kept so deletion can be retried.
              </div>
            </div>
            <div className="grid gap-2 sm:grid-cols-[minmax(180px,1fr)_auto]">
              <TextInput
                value={deleteConfirm}
                onChange={(e) => onDeleteConfirmChange(e.target.value)}
                placeholder={`type ${agent.name}`}
                disabled={deleteBusy}
                invalid={deleteConfirm !== "" && deleteConfirm !== agent.name}
                mono
                compact
              />
              <ToolbarButton
                onClick={onDelete}
                disabled={deleteConfirm !== agent.name || deleteBusy}
                variant="danger"
              >
                {deleteBusy ? "deleting..." : "delete"}
              </ToolbarButton>
            </div>
          </div>
          {deleteErr && (
            <InlineAlert tone="red" role="alert" className="mt-2 text-xs">
              {deleteErr}
            </InlineAlert>
          )}
        </InlineAlert>
      )}
    </div>
  );
}

function SelfHealingPanel({
  policy,
  history,
  busy,
  error,
}: {
  policy: MyAgentListing["self_healing"];
  history: AgentSelfHealingHistory | null;
  busy: boolean;
  error: string | null;
}) {
  const enabled = Boolean(policy?.enabled);
  const runs = history?.runs || [];
  return (
    <SurfacePanel as="section" className="mt-4 bg-runtime-panel/40 p-4">
      <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <div className="text-xs font-semibold text-ink">Self-healing</div>
          <p className="mt-1 text-xs leading-relaxed text-ink-muted">
            {enabled
              ? "Runtime failures can trigger a bounded source repair. Every error, code change, test, and exact redeploy is recorded here."
              : "Off. Add self_healing: true (or a bounded policy) to a2a.yaml to opt in."}
          </p>
        </div>
        <span
          className={`shrink-0 rounded-full border px-2 py-1 font-mono text-[10px] ${
            enabled
              ? "border-signal-success/30 text-signal-success"
              : "border-runtime-line-soft text-ink-muted"
          }`}
        >
          {enabled ? "YAML ENABLED" : "DISABLED"}
        </span>
      </div>

      {enabled && (
        <dl className="mt-3 grid gap-2 text-xs sm:grid-cols-2 lg:grid-cols-4">
          <DefinitionRow
            label="trigger"
            value={`${policy.consecutive_failures || 1} failure${(policy.consecutive_failures || 1) === 1 ? "" : "s"}`}
          />
          <DefinitionRow
            label="cooldown"
            value={`${Math.round((policy.cooldown_seconds || 900) / 60)} min`}
          />
          <DefinitionRow
            label="daily ceiling"
            value={String(policy.max_repairs_per_day || 3)}
          />
          <DefinitionRow
            label="tests"
            value={policy.require_tests === false ? "optional" : "required"}
          />
        </dl>
      )}

      {error && (
        <InlineAlert tone="red" role="alert" className="mt-3 text-xs">
          Could not load repair history: {error}
        </InlineAlert>
      )}
      {busy ? (
        <div className="mt-3 text-xs text-ink-muted">Loading repair history…</div>
      ) : runs.length === 0 ? (
        <div className="mt-3 rounded border border-runtime-line-soft/60 p-3 text-xs text-ink-muted">
          No repair attempts yet.
        </div>
      ) : (
        <div className="mt-3 grid gap-3">
          {runs.map((run) => (
            <SelfHealingRunCard key={run.job_id} run={run} />
          ))}
        </div>
      )}
    </SurfacePanel>
  );
}

function SelfHealingRunCard({ run }: { run: AgentSelfHealingRun }) {
  const failure = objectValue(run.payload?.failure);
  const result = objectValue(run.result);
  const deployment = objectValue(result.deployment);
  const testEvidence = objectValue(result.test_evidence);
  const changedFiles = arrayValue(result.changed_files);
  const tone = run.status === "complete" ? "text-signal-success" : run.status === "error" ? "text-signal-danger" : "text-signal-warning";
  return (
    <article className="rounded border border-runtime-line-soft/60 bg-runtime-base/30 p-3">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <div className={`font-mono text-[10px] uppercase ${tone}`}>{run.status}</div>
          <div className="mt-1 text-xs text-ink">
            {run.summary || run.error || "Repair in progress"}
          </div>
        </div>
        <code className="text-[10px] text-ink-dim">{run.job_id.slice(0, 12)}</code>
      </div>
      {Object.keys(failure).length > 0 && (
        <div className="mt-3 rounded border border-signal-danger/20 bg-signal-danger/5 p-2">
          <div className="font-mono text-[10px] text-signal-danger">
            {stringValue(failure.error_type) || "Runtime error"}
            {stringValue(failure.skill_name) ? ` · ${stringValue(failure.skill_name)}` : ""}
          </div>
          <pre className="mt-1 max-h-32 overflow-auto whitespace-pre-wrap break-words text-[11px] text-ink-muted">
            {stringValue(failure.error_preview) || "Error details were redacted."}
          </pre>
        </div>
      )}
      {(changedFiles.length > 0 || stringValue(result.source_head_after)) && (
        <div className="mt-3 grid gap-2 text-xs sm:grid-cols-2">
          <DefinitionRow
            label="source change"
            value={stringValue(result.source_head_after)?.slice(0, 12) || "-"}
            mono
          />
          <DefinitionRow
            label="deployment"
            value={stringValue(deployment.deploy_id) || stringValue(deployment.status) || "-"}
            mono
          />
          <DefinitionRow
            label="changed files"
            value={changedFiles.length ? changedFiles.join(", ") : "not reported"}
            mono
          />
          <DefinitionRow
            label="verified"
            value={result.healed === true ? "exact commit live" : "pending"}
          />
          <DefinitionRow
            label="tests"
            value={displayValue(testEvidence.result) || "not reported"}
            mono
          />
        </div>
      )}
      <div className="mt-3 border-t border-runtime-line-soft/50 pt-2">
        {run.events.map((event, index) => (
          <div key={event.event_id || `${run.job_id}-${index}`} className="mt-1 flex gap-2 text-[11px]">
            <span className="font-mono text-ink-faint">{String(index + 1).padStart(2, "0")}</span>
            <span className="text-ink-muted">{event.message || event.event_type || "event"}</span>
          </div>
        ))}
      </div>
    </article>
  );
}

function objectValue(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function arrayValue(value: unknown): string[] {
  return Array.isArray(value) ? value.map(String).filter(Boolean).slice(0, 200) : [];
}

function stringValue(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function displayValue(value: unknown): string {
  if (typeof value === "string") return value.slice(0, 1000);
  if (value === undefined || value === null) return "";
  try {
    return JSON.stringify(value).slice(0, 1000);
  } catch {
    return String(value).slice(0, 1000);
  }
}

function TemplateLineagePanel({
  lineage,
  busy,
  blocked,
  result,
  error,
  onRequest,
}: {
  lineage: TemplateLineageView;
  busy: boolean;
  blocked: boolean;
  result: AgentTemplateUpdateResult | null;
  error: string | null;
  onRequest: () => void;
}) {
  const policy = templateUpdatePolicy(lineage);
  const canRequest = policy !== "none" && !blocked && !busy && !result;
  return (
    <InlineAlert tone="amber" className="mt-4">
      <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0">
          <div className="text-xs font-semibold">
            Template lineage
          </div>
        </div>
        <ToolbarButton
          onClick={onRequest}
          disabled={!canRequest}
          variant="primary"
          size="md"
        >
          {templateUpdateButtonLabel({ busy, blocked, policy, queued: Boolean(result) })}
        </ToolbarButton>
      </div>
      <dl className="mt-3 grid gap-2 text-xs md:grid-cols-2">
        <DefinitionRow
          label="template"
          value={lineage.template_ref || lineage.source_agent || "-"}
          mono
        />
        <DefinitionRow
          label="version"
          value={lineage.template_version || lineage.source_agent_version || "-"}
        />
        <DefinitionRow
          label="instance"
          value={lineage.instance_version || lineage.instance_id || "-"}
        />
        <DefinitionRow label="policy" value={policy} />
        <DefinitionRow label="channel" value={lineage.update_channel || "-"} />
        <DefinitionRow label="migration" value={lineage.migration_skill || "-"} mono />
      </dl>
      {lineage.source_repo_url && (
        <div className="mt-3">
          <ToolbarLink href={lineage.source_repo_url} external size="xs">
            Source repository
          </ToolbarLink>
        </div>
      )}
      {result && (
        <InlineAlert tone="amber" className="mt-3 text-xs">
          Queued <code className="font-mono">{result.job_id}</code> with status{" "}
          <code className="font-mono">{result.status}</code>.
        </InlineAlert>
      )}
      {error && (
        <InlineAlert tone="red" role="alert" className="mt-3 text-xs">
          {error}
        </InlineAlert>
      )}
    </InlineAlert>
  );
}
