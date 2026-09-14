import type { ReactNode } from "react";
import {
  InlineAlert,
  SummaryMetric,
  SurfacePanel,
} from "../../DashboardChrome";
import { StateBadge, StatusBadge } from "../../StatusPillAdapters";
import type { ArenaScoreboardView } from "../../../arenaScoreboard";
import {
  type CustomKernelSimulationTemplate,
  type EvidenceTimelineItem,
  type EvidenceTimelineLane,
} from "../../../api";
import { fmtDate } from "../agentTypes";

export const EVIDENCE_LANES: Array<"all" | EvidenceTimelineLane> = [
  "all",
  "version",
  "authority",
  "mutation",
  "quality",
  "process",
  "cost",
  "control",
];

export type KernelTemplateSummary = Pick<
  CustomKernelSimulationTemplate,
  "template_id" | "kind" | "risk_class" | "description" | "gates"
>;

export function KernelTemplateSummaryPanel({
  template,
  emptyDescription,
}: {
  template: KernelTemplateSummary | null;
  emptyDescription: string;
}) {
  if (!template) {
    return (
      <SurfacePanel
        as="div"
        className="bg-runtime-panel/30 px-3 py-2 text-[11px] leading-relaxed text-ink-muted"
      >
        {emptyDescription}
      </SurfacePanel>
    );
  }

  return (
    <SurfacePanel
      as="div"
      className="bg-runtime-panel/30 px-3 py-2 text-[11px] text-ink-muted"
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-ink-soft">{template.template_id}</span>
        <StatusBadge tone="neutral" className="uppercase">
          {template.kind}
        </StatusBadge>
        <StatusBadge tone="amber" className="uppercase">
          {template.risk_class}
        </StatusBadge>
      </div>
      <div className="mt-1 line-clamp-2 leading-relaxed">
        {template.description}
      </div>
      {template.gates.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-1">
          {template.gates.slice(0, 3).map((gate) => (
            <StatusBadge key={gate} tone="emerald" className="text-[10px]">
              {gate}
            </StatusBadge>
          ))}
        </div>
      )}
    </SurfacePanel>
  );
}

export function EvidenceFlag({ ok, label }: { ok: boolean; label: string }) {
  return (
    <span
      className={
        ok
          ? "rounded-full border border-signal-live/45 bg-signal-live/12 px-2 py-0.5 text-signal-live"
          : "rounded-full border border-runtime-line-soft/60 bg-runtime-panel px-2 py-0.5 text-ink-muted"
      }
    >
      {label}
    </span>
  );
}

export function EvidenceMetric({
  label,
  value,
  detail,
}: {
  label: string;
  value: string;
  detail?: string;
}) {
  return (
    <SurfacePanel as="div" className="bg-runtime-panel/40 p-3">
      <div className="text-[10px] uppercase text-ink-faint">{label}</div>
      <div className="mt-1 truncate font-mono text-xs text-ink">{value}</div>
      {detail && (
        <div className="mt-1 truncate text-[11px] text-ink-muted">{detail}</div>
      )}
    </SurfacePanel>
  );
}

export function EvidenceTimelineRow({ item }: { item: EvidenceTimelineItem }) {
  return (
    <SurfacePanel
      as="article"
      className="grid gap-2 p-3 text-xs md:grid-cols-[92px_minmax(0,1fr)_112px]"
    >
      <div className="flex items-start gap-2">
        <EvidenceLanePill lane={item.lane} />
      </div>
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-mono text-ink">{item.label}</span>
          <span className="rounded-full border border-runtime-line-soft/60 px-2 py-0.5 text-[11px] text-ink-muted">
            {item.type}
          </span>
          {item.status && <StateBadge status={item.status} size="xs" />}
          {item.confidence === "inferred" && (
            <span className="text-[11px] text-signal-authority">inferred</span>
          )}
        </div>
        {item.summary && (
          <div className="mt-1 line-clamp-2 leading-relaxed text-ink-muted">
            {item.summary}
          </div>
        )}
        <div className="mt-1 truncate font-mono text-[11px] text-ink-faint">
          {item.node_id}
        </div>
      </div>
      <div className="text-left text-ink-faint md:text-right">
        {item.created_at ? fmtDate(item.created_at) : "-"}
      </div>
    </SurfacePanel>
  );
}

function EvidenceLanePill({ lane }: { lane: EvidenceTimelineLane }) {
  const tone =
    lane === "version"
      ? "border-runtime-line-soft/60 bg-runtime-bg text-ink-soft"
      : lane === "authority"
        ? "border-signal-live/45 bg-signal-live/12 text-signal-live"
        : lane === "mutation"
          ? "border-signal-authority/45 bg-signal-authority/12 text-signal-authority"
          : lane === "quality"
            ? "border-signal-authority/45 bg-signal-authority/12 text-signal-authority"
            : lane === "cost"
              ? "border-signal-protocol/40 bg-signal-protocol/10 text-signal-protocol"
              : "border-runtime-line-soft/60 bg-runtime-bg text-ink-dim";
  return (
    <span className={`rounded-full border px-2 py-0.5 text-[11px] ${tone}`}>
      {lane}
    </span>
  );
}

export function ArenaScoreboardCard({
  scoreboard,
  compact = false,
}: {
  scoreboard: ArenaScoreboardView;
  compact?: boolean;
}) {
  return (
    <SurfacePanel as="section" className="mt-3 bg-runtime-bg p-3">
      <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <div className="text-xs font-medium text-ink-soft">{scoreboard.title}</div>
          <div className="mt-1 font-mono text-[11px] text-ink-faint">
            {scoreboard.suiteId || "arena-suite"}
          </div>
        </div>
        <div className="grid grid-cols-3 gap-1.5">
          <SummaryMetric label="episodes" value={scoreboard.episodeCount} size="compact" />
          <SummaryMetric label="winners" value={scoreboard.winnerEventCount} size="compact" />
          <SummaryMetric label="fails" value={scoreboard.invariantFailureCount} size="compact" />
        </div>
      </div>

      {scoreboard.participants.length > 0 ? (
        <div className="mt-3 grid gap-2 lg:grid-cols-2">
          {scoreboard.participants.slice(0, 4).map((participant, index) => {
            const reasons = Object.entries(participant.exclusionReasons);
            return (
              <SurfacePanel
                as="article"
                key={participant.participantId}
                className="bg-runtime-panel/30 px-3 py-2"
              >
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <div className="flex items-center gap-2">
                      <StatusBadge tone="neutral" className="text-[10px]">
                        #{index + 1}
                      </StatusBadge>
                      <span className="truncate font-mono text-xs text-ink-soft">
                        {participant.participantId}
                      </span>
                    </div>
                    <div className="mt-1 text-[11px] text-ink-faint">
                      {participant.episodes.length} episodes ·{" "}
                      {participant.evidenceRefs.length} refs
                    </div>
                  </div>
                  <div className="text-right text-xs">
                    <div className="text-signal-live">{participant.wins} wins</div>
                    <div className="text-ink-faint">{participant.losses} losses</div>
                  </div>
                </div>

                <div className="mt-2 grid grid-cols-3 gap-1.5">
                  <SummaryMetric
                    label="avg score"
                    value={formatMetric(participant.averageScore)}
                    size="compact"
                  />
                  <SummaryMetric
                    label="max"
                    value={formatMetric(participant.maxScore)}
                    size="compact"
                  />
                  <SummaryMetric
                    label="eff"
                    value={formatMetric(participant.budgetEfficiency)}
                    size="compact"
                  />
                </div>

                {participant.exclusions > 0 && (
                  <div className="mt-2 flex flex-wrap gap-1">
                    {reasons.length > 0 ? (
                      reasons.map(([reason, count]) => (
                        <StatusBadge key={reason} tone="amber" className="text-[10px]">
                          {reason}: {count}
                        </StatusBadge>
                      ))
                    ) : (
                      <StatusBadge tone="amber" className="text-[10px]">
                        {participant.exclusions} exclusions
                      </StatusBadge>
                    )}
                  </div>
                )}
              </SurfacePanel>
            );
          })}
        </div>
      ) : (
        <SurfacePanel
          as="div"
          className="mt-3 bg-runtime-panel/30 px-3 py-2 text-xs text-ink-muted"
        >
          Historical summary only: detailed participant rows are redacted from this timeline view.
        </SurfacePanel>
      )}

      {compact && scoreboard.participants.length === 0 && (
        <div className="mt-2 grid grid-cols-3 gap-1.5">
          <SummaryMetric label="passed" value={scoreboard.passedEpisodeCount} size="compact" />
          <SummaryMetric label="failed" value={scoreboard.failedEpisodeCount} size="compact" />
          <SummaryMetric
            label="rejected"
            value={scoreboard.rejectedWinnerEventCount}
            size="compact"
          />
        </div>
      )}

      {scoreboard.activeApplyEnabled && (
        <InlineAlertDanger>Active apply was enabled in this payload.</InlineAlertDanger>
      )}
    </SurfacePanel>
  );
}

function InlineAlertDanger({ children }: { children: ReactNode }) {
  return (
    <InlineAlert tone="red" role="alert" className="mt-2 text-xs">
      {children}
    </InlineAlert>
  );
}

export function isActiveReviewLoop(status: string | null | undefined) {
  return ["queued", "running", "blocked"].includes(String(status || "").toLowerCase());
}

export function objectValue(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

export function textValue(value: unknown): string {
  return typeof value === "string" || typeof value === "number"
    ? String(value)
    : "";
}

export function numberValue(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

export function booleanValue(value: unknown): boolean {
  return value === true;
}

export function shortRef(value: string): string {
  return value.length > 14 ? value.slice(0, 12) : value;
}

function formatMetric(value: number): string {
  if (!Number.isFinite(value)) return "0";
  if (Math.abs(value) >= 100) return value.toFixed(0);
  if (Math.abs(value) >= 10) return value.toFixed(1);
  return value.toFixed(2).replace(/\.?0+$/, "");
}

export const DEFAULT_CUSTOM_KERNEL_SIMULATION_SPEC_TEXT = JSON.stringify(
  {
    title: "Competitive allocation drill",
    actors: [{ id: "market" }, { id: "bidder-a" }, { id: "bidder-b" }],
    ports: [
      { node_id: "market", id: "route:bid", direction: "output", schema_ref: "route.v1" },
      { node_id: "bidder-a", id: "invoke:bid", direction: "input", schema_ref: "skill.v1" },
      { node_id: "bidder-b", id: "invoke:bid", direction: "input", schema_ref: "skill.v1" },
    ],
    capabilities: [
      {
        id: "cap-market",
        owner: "market",
        actions: ["call", "review"],
        resources: ["skill:*", "agent:*", "bidder-*:*"],
        budget: 12,
        delegation_depth: 2,
      },
    ],
    policies: [
      {
        id: "owner-allow-call",
        level: "owner",
        effect: "allow",
        actions: ["call"],
        resources: ["skill:*", "bidder-*:invoke:*"],
      },
    ],
    steps: [
      {
        type: "delegate",
        parent_capability_id: "cap-market",
        child_capability_id: "cap-bidder-a",
        child_owner: "bidder-a",
        requested: {
          actions: ["call"],
          resources: ["skill:bid", "bidder-a:invoke:*"],
          budget: 5,
          delegation_depth: 1,
        },
      },
      {
        type: "delegate",
        parent_capability_id: "cap-market",
        child_capability_id: "cap-bidder-b",
        child_owner: "bidder-b",
        requested: {
          actions: ["call"],
          resources: ["skill:bid", "bidder-b:invoke:*"],
          budget: 5,
          delegation_depth: 1,
        },
      },
      {
        type: "start_process",
        process_id: "allocation-process-1",
        owner: "market",
        capability_id: "cap-market",
        ttl: 10,
        budget: 10,
      },
      {
        type: "propose_edge",
        edge_id: "edge-market-bidder-a",
        from: { node_id: "market", port_id: "route:bid" },
        to: { node_id: "bidder-a", port_id: "invoke:bid" },
        edge_type: "call",
        capability_id: "cap-bidder-a",
        process_id: "allocation-process-1",
        provenance_ref: "template:competitive_allocation@v1",
      },
      {
        type: "activate_edge",
        edge_id: "edge-market-bidder-a",
        decision_id: "pd-edge-bidder-a",
      },
      {
        type: "use_edge",
        edge_id: "edge-market-bidder-a",
        cost: 1,
      },
      {
        type: "propose_edge",
        edge_id: "edge-market-bidder-b",
        from: { node_id: "market", port_id: "route:bid" },
        to: { node_id: "bidder-b", port_id: "invoke:bid" },
        edge_type: "call",
        capability_id: "cap-bidder-b",
        process_id: "allocation-process-1",
        provenance_ref: "template:competitive_allocation@v1",
      },
      {
        type: "activate_edge",
        edge_id: "edge-market-bidder-b",
        decision_id: "pd-edge-bidder-b",
      },
      {
        type: "use_edge",
        edge_id: "edge-market-bidder-b",
        cost: 1,
      },
      {
        type: "emit_signal",
        node_id: "bidder-a",
        signal_type: "success",
        payload: { score: 1 },
      },
      {
        type: "emit_signal",
        node_id: "bidder-b",
        signal_type: "failure",
        payload: { score: 0 },
      },
      {
        type: "select_route",
        skill: "bid",
        candidates: {
          "bidder-a": "cap-bidder-a",
          "bidder-b": "cap-bidder-b",
        },
      },
      {
        type: "check_policy",
        decision_id: "pd-bid",
        action: "call",
        resource: "skill:bid",
      },
      {
        type: "stop_process",
        process_id: "allocation-process-1",
        reason: "simulation_completed",
      },
    ],
    invariants: [
      "replay_deterministic",
      "no_active_apply",
      "no_violations",
      { id: "edge_expired", edge_id: "edge-market-bidder-a" },
      { id: "edge_expired", edge_id: "edge-market-bidder-b" },
      { id: "no_active_process_edges", process_id: "allocation-process-1" },
      { id: "expected_decision", step: 13, decision: "allow" },
    ],
  },
  null,
  2,
);
