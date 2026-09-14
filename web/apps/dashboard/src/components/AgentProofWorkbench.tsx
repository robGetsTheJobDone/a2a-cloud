import { useEffect, useMemo, useState } from "react";
import { trackEvent } from "../analytics";
import {
  runAgentProof,
  type AgentListing,
  type AgentProofRun,
  type AgentSkill,
} from "../api";
import { NoAgentProofsState } from "./AgentLifecycleEmptyStates";
import {
  CodeBlock,
  CopyButton,
  copyTextToClipboard,
  FormField,
  InlineAlert,
  SelectInput,
  SelectableSurfaceLink,
  SummaryMetric,
  SurfacePanel,
  TextArea,
  ToolbarButton,
  ToolbarLink,
} from "./DashboardChrome";
import { StateBadge } from "./StatusPillAdapters";

export type AgentProofWorkbenchProps = {
  agent: AgentListing;
  proofs: AgentProofRun[];
  selectedProofId?: string | null;
  search?: string;
  onProofCreated: (proof: AgentProofRun) => void;
};

function agentProofsRoute(agentName: string, search = "") {
  return `/my-agents/${encodeURIComponent(agentName)}/proofs${search}`;
}

function agentProofRoute(agentName: string, proofId: string | number, search = "") {
  return `/my-agents/${encodeURIComponent(agentName)}/proofs/${encodeURIComponent(String(proofId))}${search}`;
}

export function AgentProofWorkbench({
  agent,
  proofs,
  selectedProofId,
  search = "",
  onProofCreated,
}: AgentProofWorkbenchProps) {
  const skills = useMemo(() => agent.card?.skills || [], [agent.card?.skills]);
  const sortedProofs = useMemo(
    () =>
      proofs
        .filter((proof) => proof.agent_name === agent.name)
        .slice()
        .sort((a, b) => dateMs(b.created_at) - dateMs(a.created_at)),
    [agent.name, proofs],
  );
  const latestProof = sortedProofs[0] || null;
  const selectedProof = selectedProofId
    ? sortedProofs.find((proof) => String(proof.id) === selectedProofId) || null
    : null;
  const [skillName, setSkillName] = useState(skills[0]?.name || "");
  const selectedSkill =
    skills.find((skill) => skill.name === skillName) || skills[0] || null;
  const [argsJson, setArgsJson] = useState(() =>
    JSON.stringify(sampleArgs(selectedSkill), null, 2),
  );
  const [dirtyArgs, setDirtyArgs] = useState(false);
  const [busy, setBusy] = useState(false);
  const [runError, setRunError] = useState<string | null>(null);

  useEffect(() => {
    setSkillName((current) => {
      if (skills.some((skill) => skill.name === current)) return current;
      return skills[0]?.name || "";
    });
  }, [skills]);

  useEffect(() => {
    if (!dirtyArgs) {
      setArgsJson(JSON.stringify(sampleArgs(selectedSkill), null, 2));
    }
  }, [dirtyArgs, selectedSkill]);

  const argsValidation = useMemo(() => validateArgs(argsJson), [argsJson]);
  const canRun = Boolean(selectedSkill && argsValidation.ok && !busy);

  function selectSkill(nextSkillName: string) {
    const nextSkill =
      skills.find((skill) => skill.name === nextSkillName) || null;
    setSkillName(nextSkillName);
    setArgsJson(JSON.stringify(sampleArgs(nextSkill), null, 2));
    setDirtyArgs(false);
    setRunError(null);
  }

  function resetArgs() {
    setArgsJson(JSON.stringify(sampleArgs(selectedSkill), null, 2));
    setDirtyArgs(false);
    setRunError(null);
  }

  async function runProof() {
    if (!selectedSkill || !argsValidation.ok || busy) return;
    setBusy(true);
    setRunError(null);
    try {
      const proof = await runAgentProof(agent.name, {
        skill_name: selectedSkill.name,
        args: argsValidation.value,
        args_json: argsJson,
      });
      onProofCreated(proof);
      setDirtyArgs(false);
    } catch (ex) {
      setRunError(ex instanceof Error ? ex.message : String(ex));
    } finally {
      setBusy(false);
    }
  }

  return (
    <SurfacePanel as="section" className="bg-runtime-bg p-4">
      <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
        <div className="min-w-0">
          <div className="text-[10px] uppercase text-ink-faint">
            Proof workbench
          </div>
          <div className="mt-1 flex flex-wrap items-center gap-2">
            <h3 className="font-mono text-sm font-semibold text-ink">
              {agent.name}
            </h3>
            <StateBadge
              status={latestProof?.badge || "unverified"}
              aria-label={`Proof status: ${latestProof?.badge || "unverified"}`}
              size="xs"
            />
            {latestProof && (
              <StateBadge status={latestProof.status} size="xs" />
            )}
          </div>
          <p className="mt-2 max-w-2xl text-sm leading-relaxed text-ink-dim">
            {agent.card?.description || agent.description || "No description."}
          </p>
        </div>

        <div className="grid w-full grid-cols-3 gap-2 text-xs sm:w-[360px]">
          <SummaryMetric label="tools" value={skills.length} />
          <SummaryMetric label="proofs" value={sortedProofs.length} />
          <SummaryMetric
            label="latest"
            value={latestProof ? fmtDate(latestProof.created_at) : "none"}
          />
        </div>
      </div>

      <div
        className={
          "mt-4 grid gap-4 border-t border-runtime-line-soft/60 pt-4 " +
          (selectedProofId
            ? "lg:grid-cols-[minmax(260px,360px)_minmax(0,1fr)]"
            : "lg:grid-cols-[minmax(0,1fr)_360px]")
        }
      >
        <div className="min-w-0">
          {sortedProofs.length === 0 ? (
            <NoAgentProofsState
              size="comfortable"
              action={
                <ToolbarButton
                  type="button"
                  onClick={runProof}
                  disabled={!canRun}
                  variant="primary"
                  size="sm"
                >
                  {busy ? "Running..." : "Run the first proof"}
                </ToolbarButton>
              }
            />
          ) : (
            <>
              <div className="text-[10px] uppercase text-ink-faint">
                Latest receipt
              </div>
              {latestProof && (
                <LatestProofSummary
                  agentName={agent.name}
                  agentPublic={agent.public}
                  showShare={!selectedProofId}
                  proof={latestProof}
                  href={agentProofRoute(agent.name, latestProof.id, search)}
                />
              )}

              <div className="mt-4 text-[10px] uppercase text-ink-faint">
                Proof history
              </div>
              <div className="mt-2 space-y-2">
                {sortedProofs.slice(0, 6).map((proof) => (
                  <ProofHistoryRow
                    key={proof.id}
                    proof={proof}
                    href={agentProofRoute(agent.name, proof.id, search)}
                    selected={String(proof.id) === selectedProofId}
                  />
                ))}
              </div>
            </>
          )}
        </div>

        <div className="grid gap-3">
          {selectedProofId && (
            <ProofDetailPanel
              agentName={agent.name}
              agentPublic={agent.public}
              proof={selectedProof}
              proofId={selectedProofId}
              allProofsHref={agentProofsRoute(agent.name, search)}
            />
          )}

          <div className="grid gap-2">
          <FormField label="Tool">
            <SelectInput
              value={selectedSkill?.name || ""}
              onChange={(e) => selectSkill(e.target.value)}
              disabled={skills.length === 0 || busy}
            >
              {skills.length === 0 ? (
                <option value="">no tools</option>
              ) : (
                skills.map((skill) => (
                  <option key={skill.name} value={skill.name}>
                    {skill.name}
                  </option>
                ))
              )}
            </SelectInput>
          </FormField>

          {selectedSkill?.description && (
            <InlineAlert tone="neutral" className="text-xs leading-relaxed">
              {selectedSkill.description}
            </InlineAlert>
          )}

          <div className="flex items-center justify-between gap-3">
            <span className="text-[10px] uppercase text-ink-faint">
              Sample args
            </span>
            <ToolbarButton
              type="button"
              onClick={resetArgs}
              disabled={!selectedSkill || busy}
              size="xs"
            >
              Reset
            </ToolbarButton>
          </div>
          <TextArea
            value={argsJson}
            onChange={(e) => {
              setArgsJson(e.target.value);
              setDirtyArgs(true);
              setRunError(null);
            }}
            rows={8}
            spellCheck={false}
            disabled={!selectedSkill || busy}
            invalid={!argsValidation.ok}
            mono
            className="min-h-[192px] resize-none text-xs"
          />

          {!argsValidation.ok && (
            <InlineAlert tone="red" className="text-xs">{argsValidation.error}</InlineAlert>
          )}
          {runError && (
            <InlineAlert tone="red" className="text-xs">{runError}</InlineAlert>
          )}

          <ToolbarButton
            type="button"
            onClick={runProof}
            disabled={!canRun}
            variant="primary"
            size="md"
          >
            {busy ? "Running proof..." : "Run proof receipt"}
          </ToolbarButton>
          </div>
        </div>
      </div>
    </SurfacePanel>
  );
}

function LatestProofSummary({
  agentName,
  agentPublic,
  showShare,
  proof,
  href,
}: {
  agentName: string;
  agentPublic: boolean;
  showShare: boolean;
  proof: AgentProofRun;
  href: string;
}) {
  const grantPrefix = proof.grant_id ? proof.grant_id.slice(0, 12) : null;
  const fileOpsCount = proof.file_ops_count ?? proof.file_ops.length;
  const eventsCount = proof.events_count ?? proof.events.length;
  return (
    <SurfacePanel as="div" className="mt-2 bg-runtime-panel/40 p-3 text-xs">
      <div className="flex flex-wrap items-center gap-2">
        <StateBadge
          status={proof.badge}
          aria-label={`Proof status: ${proof.badge}`}
          size="xs"
        />
        <span className="font-mono text-ink-soft">{proof.skill_name}</span>
        <StateBadge status={proof.status} size="xs" />
        {proof.elapsed_ms !== null && (
          <span className="text-ink-faint">{fmtMs(proof.elapsed_ms)}</span>
        )}
        {proof.head_sha && (
          <span className="font-mono text-ink-faint">
            {proof.head_sha.slice(0, 8)}
          </span>
        )}
      </div>
      <div className="mt-2 text-ink-dim">
        {proof.summary || proof.error || "No summary."}
      </div>
      <div className="mt-2 flex flex-wrap gap-3 text-[11px] text-ink-faint">
        <span>{fileOpsCount} file ops</span>
        <span>{eventsCount} events</span>
        <span>{fmtDateTime(proof.created_at)}</span>
        {grantPrefix && <span className="font-mono">{grantPrefix}</span>}
        <ToolbarLink href={href} size="xs">
          Open receipt
        </ToolbarLink>
        {showShare && canShareProofDrop(agentPublic, proof.status) && (
          <ProofDropButton agentName={agentName} proof={proof} />
        )}
      </div>
      <dl className="mt-3 grid gap-2 border-t border-runtime-line-soft/60 pt-3 sm:grid-cols-2">
        <EvidenceRow
          label="proof id"
          value={`proof/${proof.id}`}
          copyValue={String(proof.id)}
        />
        <EvidenceRow
          label="grant"
          value={grantPrefix || "none"}
          copyValue={grantPrefix}
        />
        <EvidenceRow
          label="head"
          value={proof.head_sha ? proof.head_sha.slice(0, 12) : "none"}
          copyValue={proof.head_sha}
        />
        <EvidenceRow
          label="card"
          value={proof.card_hash ? proof.card_hash.slice(0, 12) : "none"}
          copyValue={proof.card_hash}
        />
      </dl>
    </SurfacePanel>
  );
}

function ProofHistoryRow({
  proof,
  href,
  selected,
}: {
  proof: AgentProofRun;
  href: string;
  selected: boolean;
}) {
  return (
    <SelectableSurfaceLink
      href={href}
      selected={selected}
      className="bg-runtime-panel/30 p-3 text-xs"
    >
      <div className="grid gap-2 sm:grid-cols-[110px_minmax(0,1fr)_80px] sm:items-center">
        <div className="flex items-center gap-2">
          <StateBadge
            status={proof.badge}
            aria-label={`Proof status: ${proof.badge}`}
            size="xs"
          />
          <StateBadge status={proof.status} size="xs" />
        </div>
        <div className="min-w-0">
          <div className="truncate font-mono text-ink-soft">
            {proof.skill_name}
          </div>
          <div className="mt-1 truncate text-ink-muted">
            {proof.summary || proof.error || "No summary."}
          </div>
        </div>
        <div className="text-ink-faint">{fmtDate(proof.created_at)}</div>
      </div>
    </SelectableSurfaceLink>
  );
}

function ProofDetailPanel({
  agentName,
  agentPublic,
  proof,
  proofId,
  allProofsHref,
}: {
  agentName: string;
  agentPublic: boolean;
  proof: AgentProofRun | null;
  proofId: string;
  allProofsHref: string;
}) {
  if (!proof) {
    return (
      <SurfacePanel as="section" className="bg-runtime-bg p-3">
        <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
          <div className="min-w-0">
            <div className="text-[10px] uppercase text-ink-faint">
              Proof receipt
            </div>
            <h4 className="mt-1 font-mono text-sm text-ink [overflow-wrap:anywhere]">
              proof/{proofId}
            </h4>
          </div>
          <ToolbarLink href={allProofsHref} size="xs">
            All proofs
          </ToolbarLink>
        </div>
        <InlineAlert tone="red" className="mt-3 text-xs">
          This proof receipt was not found in the loaded proof history.
        </InlineAlert>
      </SurfacePanel>
    );
  }

  const fileOpsCount = proof.file_ops_count ?? proof.file_ops.length;
  const eventsCount = proof.events_count ?? proof.events.length;
  return (
    <SurfacePanel as="section" className="min-w-0 bg-runtime-bg p-3">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0">
          <div className="text-[10px] uppercase text-ink-faint">
            Proof receipt
          </div>
          <div className="mt-1 flex flex-wrap items-center gap-2">
            <h4 className="font-mono text-sm font-semibold text-ink">
              proof/{proof.id}
            </h4>
            <StateBadge status={proof.badge} size="xs" />
            <StateBadge status={proof.status} size="xs" />
          </div>
          <div className="mt-1 text-xs text-ink-muted [overflow-wrap:anywhere]">
            {proof.agent_name}.{proof.skill_name}
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {canShareProofDrop(agentPublic, proof.status) && (
            <ProofDropButton agentName={agentName} proof={proof} />
          )}
          <ToolbarLink href={allProofsHref} size="xs">
            All proofs
          </ToolbarLink>
        </div>
      </div>

      <div className="mt-3 grid gap-2 sm:grid-cols-4">
        <SummaryMetric label="files" value={fileOpsCount} size="compact" />
        <SummaryMetric label="events" value={eventsCount} size="compact" />
        <SummaryMetric label="elapsed" value={proof.elapsed_ms === null ? "-" : fmtMs(proof.elapsed_ms)} size="compact" />
        <SummaryMetric label="created" value={fmtDateTime(proof.created_at)} size="compact" />
      </div>

      {proof.error ? (
        <InlineAlert tone="red" role="alert" className="mt-3 text-xs [overflow-wrap:anywhere]">
          {proof.error}
        </InlineAlert>
      ) : proof.summary ? (
        <SurfacePanel as="div" className="mt-3 bg-runtime-panel/50 p-3 text-sm text-ink-soft [overflow-wrap:anywhere]">
          {proof.summary}
        </SurfacePanel>
      ) : null}

      <dl className="mt-3 grid gap-2 sm:grid-cols-2">
        <EvidenceRow label="grant" value={proof.grant_id || "none"} copyValue={proof.grant_id} />
        <EvidenceRow label="head" value={proof.head_sha || "none"} copyValue={proof.head_sha} />
        <EvidenceRow label="card" value={proof.card_hash || "none"} copyValue={proof.card_hash} />
        <EvidenceRow label="completed" value={proof.completed_at ? fmtDateTime(proof.completed_at) : "-"} />
      </dl>

      <div className="mt-3 grid gap-3 xl:grid-cols-2">
        <ProofJsonBlock label="Args preview" value={proof.args_preview} />
        <ProofJsonBlock label="Result" value={proof.result} />
        {proof.file_ops.length > 0 && (
          <ProofJsonBlock label="File operations" value={proof.file_ops} />
        )}
        {proof.events.length > 0 && (
          <ProofJsonBlock label="Events" value={proof.events} />
        )}
      </div>
    </SurfacePanel>
  );
}

function ProofDropButton({
  agentName,
  proof,
}: {
  agentName: string;
  proof: AgentProofRun;
}) {
  const [label, setLabel] = useState("Share ProofDrop");

  async function shareProofDrop() {
    const url = proofDropPublicUrl(agentName, proof.id);
    const shareData = {
      title: `${agentName}.${proof.skill_name} — verified ProofDrop`,
      text: `${agentName}.${proof.skill_name} completed a platform-verified run on a2a cloud.`,
      url,
    };
    try {
      if (navigator.share) {
        await navigator.share(shareData);
        setLabel("Shared");
        trackEvent("proof_drop_shared", {
          agent_name: agentName,
          proof_id: proof.id,
          skill_name: proof.skill_name,
          method: "native",
        });
      } else {
        await copyTextToClipboard(url);
        setLabel("Link copied");
        trackEvent("proof_drop_shared", {
          agent_name: agentName,
          proof_id: proof.id,
          skill_name: proof.skill_name,
          method: "copy",
        });
      }
      window.setTimeout(() => setLabel("Share ProofDrop"), 1800);
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") return;
      setLabel("Share failed");
      window.setTimeout(() => setLabel("Share ProofDrop"), 1800);
    }
  }

  return (
    <ToolbarButton
      type="button"
      size="xs"
      variant="primary"
      onClick={() => void shareProofDrop()}
      title="Shares safe proof metadata only — never prompts, raw results, events, or file paths."
    >
      {label}
    </ToolbarButton>
  );
}

export function canShareProofDrop(
  agentPublic: boolean,
  proofStatus: string,
  publicSiteUrl = configuredPublicSiteUrl(),
): boolean {
  return Boolean(publicSiteUrl) && agentPublic && proofStatus === "passed";
}

export function proofDropPublicUrl(
  agentName: string,
  proofId: string | number,
  baseUrl = configuredPublicSiteUrl(),
): string {
  const base = baseUrl.replace(/\/+$/, "");
  return `${base}/p/${encodeURIComponent(agentName)}/${encodeURIComponent(String(proofId))}`;
}

function configuredPublicSiteUrl(): string {
  const env = (import.meta as ImportMeta & {
    env?: { VITE_PUBLIC_SITE_URL?: string };
  }).env;
  const fromMeta = env?.VITE_PUBLIC_SITE_URL?.trim();
  if (fromMeta) return fromMeta;
  // Test runners (vitest) stub env vars on process.env.
  const proc = (globalThis as { process?: { env?: Record<string, string | undefined> } }).process;
  return proc?.env?.VITE_PUBLIC_SITE_URL?.trim() || "";
}

function ProofJsonBlock({ label, value }: { label: string; value: unknown }) {
  return (
    <CodeBlock label={label} className="max-h-72 p-2 text-[11px] text-ink-dim">
      {JSON.stringify(value, null, 2)}
    </CodeBlock>
  );
}

function sampleArgs(skill: AgentSkill | null): Record<string, unknown> {
  const schema = skill?.input_schema;
  if (!schema || typeof schema !== "object") return {};
  const value = sampleValue(schema);
  return isRecord(value) ? value : {};
}

function sampleValue(schema: Record<string, unknown>): unknown {
  if ("default" in schema) return schema.default;
  if ("const" in schema) return schema.const;

  const enumValues = schema.enum;
  if (Array.isArray(enumValues) && enumValues.length > 0) return enumValues[0];

  const composed = firstSchema(schema.oneOf) || firstSchema(schema.anyOf);
  if (composed) return sampleValue(composed);

  let typ = schema.type;
  if (Array.isArray(typ)) typ = typ.find((item) => item !== "null") || typ[0];

  const props = schema.properties;
  if (typ === "object" || isRecord(props)) {
    const properties = isRecord(props) ? props : {};
    const required = Array.isArray(schema.required)
      ? schema.required.filter((item): item is string => typeof item === "string")
      : Object.keys(properties);
    return required.reduce<Record<string, unknown>>((acc, key) => {
      const child = properties[key];
      if (isRecord(child)) acc[key] = sampleValue(child);
      return acc;
    }, {});
  }

  if (typ === "array") {
    const items = schema.items;
    return isRecord(items) ? [sampleValue(items)] : [];
  }
  if (typ === "integer") return 1;
  if (typ === "number") return 1;
  if (typ === "boolean") return true;
  if (typ === "string") return stringSample(schema);
  return null;
}

function firstSchema(value: unknown): Record<string, unknown> | null {
  if (!Array.isArray(value)) return null;
  const first = value.find(isRecord);
  return first || null;
}

function stringSample(schema: Record<string, unknown>): string {
  if (schema.format === "email") return "user@example.com";
  if (schema.format === "uri" || schema.format === "url") {
    return "https://example.com";
  }
  if (schema.format === "date") return "2026-05-20";
  if (schema.format === "date-time") return "2026-05-20T12:00:00Z";
  return "sample";
}

type ArgsValidation =
  | { ok: true; value: Record<string, unknown> }
  | { ok: false; error: string };

function validateArgs(value: string): ArgsValidation {
  if (!value.trim()) return { ok: true, value: {} };
  try {
    const parsed: unknown = JSON.parse(value);
    if (!isRecord(parsed)) {
      return { ok: false, error: "Proof args must be a JSON object." };
    }
    return { ok: true, value: parsed };
  } catch (ex) {
    return {
      ok: false,
      error: ex instanceof Error ? ex.message : "Invalid JSON.",
    };
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function EvidenceRow({
  label,
  value,
  copyValue,
}: {
  label: string;
  value: string;
  copyValue?: string | null;
}) {
  return (
    <div className="min-w-0">
      <dt className="text-[10px] uppercase text-ink-faint">
        {label}
      </dt>
      <dd className="mt-1 flex min-w-0 items-center gap-2">
        <span className="min-w-0 flex-1 truncate font-mono text-[11px] text-ink-dim">
          {value}
        </span>
        {copyValue && <CopyButton value={copyValue} label={`Copy ${label}`} />}
      </dd>
    </div>
  );
}

function fmtDate(value: string): string {
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return "unknown";
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function fmtDateTime(value: string): string {
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return "unknown";
  return d.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

function fmtMs(ms: number): string {
  if (ms < 1000) return `${ms}ms`;
  return `${Math.round(ms / 100) / 10}s`;
}

function dateMs(value: string): number {
  const ms = new Date(value).getTime();
  return Number.isNaN(ms) ? 0 : ms;
}
