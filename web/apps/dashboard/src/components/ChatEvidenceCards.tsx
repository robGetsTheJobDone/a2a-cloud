import { Suspense, type ReactNode } from "react";
import { StatusPill, type StatusPillTone } from "@a2a/design-system";
import type { ChatEvent } from "../api";
import type {
  ArenaScoreboardParticipant,
  ArenaScoreboardView,
} from "../arenaScoreboard";
import {
  parseChatEvidenceCards,
  type ChatEvidenceCard,
} from "../chatEvidence";
import { SurfacePanel } from "./DashboardChrome";
import {
  KernelTraceCard,
  KernelTraceGraphNode,
  KernelTraceRail,
  KernelTraceSegment,
  KernelTraceStat,
} from "./KernelTraceCard";

export type EvidenceStreamEvent = Extract<ChatEvent, { type: "evidence_event" }>;

// Evidence badges speak the Runtime-Telemetry signal vocabulary directly.
type EvidenceTone = "neutral" | "emerald" | "amber" | "red";

const EVIDENCE_TONE_TO_PILL: Record<EvidenceTone, StatusPillTone> = {
  neutral: "neutral",
  emerald: "live",
  amber: "authority",
  red: "danger",
};

function EvidenceBadge({
  tone = "neutral",
  className,
  children,
}: {
  tone?: EvidenceTone;
  className?: string;
  children: ReactNode;
}) {
  return (
    <StatusPill
      tone={EVIDENCE_TONE_TO_PILL[tone]}
      size="xs"
      dot={false}
      className={className}
    >
      {children}
    </StatusPill>
  );
}

// One cohesive loading affordance for the whole evidence list while any lazy
// KernelTraceCard body resolves. KernelTraceCard ships its own inner Suspense,
// but wrapping the list keeps a single skeleton instead of staggered pops.
function EvidenceSkeleton() {
  return (
    <SurfacePanel
      as="section"
      role="status"
      aria-label="Loading evidence"
      aria-busy="true"
      className="mt-3 animate-pulse space-y-3 bg-runtime-bg/80 p-3"
    >
      <div className="flex items-center justify-between gap-3">
        <div className="h-4 w-40 rounded bg-runtime-line/70" />
        <div className="h-4 w-24 rounded bg-runtime-line/50" />
      </div>
      <div className="grid gap-2 sm:grid-cols-3">
        <div className="h-10 rounded bg-runtime-panel/60" />
        <div className="h-10 rounded bg-runtime-panel/60" />
        <div className="h-10 rounded bg-runtime-panel/60" />
      </div>
    </SurfacePanel>
  );
}

export function LiveEvidenceCard({ ev }: { ev: EvidenceStreamEvent }) {
  const cards = parseChatEvidenceCards(ev);
  if (cards.length === 0) return null;
  return (
    <div className="flex justify-start">
      <div className="w-full max-w-[92%]">
        <ChatEvidenceCards cards={cards} />
      </div>
    </div>
  );
}

export function ChatEvidenceCards({
  cards,
  compact = false,
}: {
  cards: ChatEvidenceCard[];
  compact?: boolean;
}) {
  if (cards.length === 0) return null;
  return (
    <Suspense fallback={<EvidenceSkeleton />}>
      {cards.map((card, index) => {
        if (card.kind === "arena_suite") {
          return (
            <ArenaSuiteChatCard
              key={`arena-suite:${card.evidenceKey || card.scoreboard.suiteId || index}`}
              scoreboard={card.scoreboard}
              compact={compact}
            />
          );
        }
        if (card.kind === "kernel_signal") {
          return (
            <KernelSignalCard
              key={`kernel-signal:${card.evidenceKey || card.signal.eventType || index}`}
              signal={card.signal}
              compact={compact}
            />
          );
        }
        return (
          <KernelTraceCard
            key={`kernel-trace:${card.evidenceKey || card.trace.protocolId || index}`}
            trace={card.trace}
            compact={compact}
          />
        );
      })}
    </Suspense>
  );
}

function KernelSignalCard({
  signal,
  compact = false,
}: {
  signal: Extract<ChatEvidenceCard, { kind: "kernel_signal" }>["signal"];
  compact?: boolean;
}) {
  const isCritical =
    signal.evidenceKind === "invariant_failure" ||
    (signal.evidenceKind === "policy_decision" &&
      signal.stats.some(
        (stat) =>
          stat.label === "decision" &&
          (stat.value === "deny" || stat.value === "blocked"),
      )) ||
    signal.severity === "critical" ||
    signal.severity === "error";
  const isLimit = signal.evidenceKind === "protocol_limit";
  const isPolicyDecision = signal.evidenceKind === "policy_decision";
  const signalTone: EvidenceTone = isCritical
    ? "red"
    : isLimit
      ? "amber"
      : isPolicyDecision
        ? "emerald"
        : "neutral";
  return (
    <SurfacePanel
      as="section"
      className={`mt-3 bg-runtime-bg/80 p-3 ${compact ? "text-xs" : ""}`}
      aria-label="Kernel evidence signal"
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <EvidenceBadge tone={signalTone} className="rounded-md font-semibold">
              {signal.evidenceKind.replace(/_/g, " ")}
            </EvidenceBadge>
            <span className="break-all font-mono text-[11px] text-ink-muted">
              {signal.eventType}
            </span>
            {signal.sourceLabel && (
              <span className="text-[11px] text-ink-muted">{signal.sourceLabel}</span>
            )}
          </div>
          <div className="mt-2 text-sm font-semibold text-ink">
            {signal.title}
          </div>
          {signal.message && (
            <div className="mt-1 text-xs text-ink-dim">{signal.message}</div>
          )}
        </div>
        {(signal.status || signal.severity) && (
          <div className="flex flex-wrap justify-end gap-1.5 text-[10px]">
            {signal.status && (
              <EvidenceBadge tone="neutral" className="rounded-md text-[10px]">
                {signal.status}
              </EvidenceBadge>
            )}
            {signal.severity && (
              <EvidenceBadge tone="neutral" className="rounded-md text-[10px]">
                {signal.severity}
              </EvidenceBadge>
            )}
          </div>
        )}
      </div>

      {signal.stats.length > 0 && (
        <div className="mt-3 flex flex-wrap items-center gap-1.5 text-[10px]">
          {signal.stats.map((stat) => (
            <KernelTraceSegment
              key={`${stat.label}:${stat.value}`}
              label={stat.label}
              value={stat.value}
              tone={stat.tone}
            />
          ))}
        </div>
      )}
    </SurfacePanel>
  );
}

function ArenaSuiteChatCard({
  scoreboard,
  compact = false,
}: {
  scoreboard: ArenaScoreboardView;
  compact?: boolean;
}) {
  const scoreboardTone: EvidenceTone = scoreboard.passed ? "emerald" : "red";
  const riskCount =
    scoreboard.failedEpisodeCount +
    scoreboard.invariantFailureCount +
    scoreboard.rejectedWinnerEventCount;
  const participantCount = scoreboard.participants.length;
  const shownParticipants = scoreboard.participants.slice(0, compact ? 2 : 4);
  return (
    <SurfacePanel
      as="section"
      className={`mt-3 bg-[#100b17] p-3 ${compact ? "text-xs" : ""}`}
      aria-label="Arena suite scoreboard"
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <EvidenceBadge tone={scoreboardTone} className="rounded-md font-semibold">
              {scoreboard.passed ? "arena passed" : "arena attention"}
            </EvidenceBadge>
            {scoreboard.suiteId && (
              <span className="break-all font-mono text-[11px] text-ink-muted">
                {scoreboard.suiteId}
              </span>
            )}
            {scoreboard.activeApplyEnabled && (
              <EvidenceBadge tone="red" className="rounded-md text-[10px] uppercase">
                active apply
              </EvidenceBadge>
            )}
          </div>
          <div className="mt-2 text-sm font-semibold text-ink">
            {scoreboard.title}
          </div>
        </div>
        <div className="grid grid-cols-3 gap-1.5 text-right sm:min-w-[220px]">
          <KernelTraceStat label="episodes" value={scoreboard.episodeCount || "-"} />
          <KernelTraceStat label="winners" value={scoreboard.winnerEventCount || "-"} />
          <KernelTraceStat
            label="issues"
            value={riskCount || "-"}
            tone={riskCount > 0 ? "red" : "emerald"}
          />
        </div>
      </div>

      <div className="mt-3 grid gap-2 sm:grid-cols-3">
        <KernelTraceGraphNode
          label="episodes"
          value={`${scoreboard.passedEpisodeCount}/${scoreboard.episodeCount || 0} pass`}
          tone={scoreboard.failedEpisodeCount ? "red" : "emerald"}
        />
        <KernelTraceGraphNode
          label="selection"
          value={`${scoreboard.winnerEventCount} winners`}
          tone={scoreboard.rejectedWinnerEventCount ? "amber" : "cyan"}
        />
        <KernelTraceGraphNode
          label="gate"
          value={
            scoreboard.invariantFailureCount
              ? `${scoreboard.invariantFailureCount} failed`
              : "clear"
          }
          tone={scoreboard.invariantFailureCount ? "red" : "emerald"}
        />
      </div>

      {participantCount > 0 ? (
        <div className="mt-3 grid gap-2 sm:grid-cols-2">
          {shownParticipants.map((participant) => (
            <ArenaParticipantRow
              key={participant.participantId}
              participant={participant}
            />
          ))}
        </div>
      ) : (
        <SurfacePanel as="div" className="mt-3 bg-runtime-bg/60 px-3 py-2 text-[11px] text-ink-muted">
          Historical summary only: participant rows are not attached to this chat event.
        </SurfacePanel>
      )}

      {participantCount > shownParticipants.length && (
        <SurfacePanel as="div" className="mt-2 bg-runtime-bg/60 px-2 py-1.5 text-[11px] text-ink-muted">
          + {participantCount - shownParticipants.length} more participants summarized
        </SurfacePanel>
      )}

      {(scoreboard.failedEpisodeCount > 0 ||
        scoreboard.invariantFailureCount > 0 ||
        scoreboard.rejectedWinnerEventCount > 0) && (
        <div className="mt-3 flex flex-wrap gap-1.5">
          {scoreboard.failedEpisodeCount > 0 && (
            <ArenaRiskBadge label={`${scoreboard.failedEpisodeCount} failed episodes`} />
          )}
          {scoreboard.invariantFailureCount > 0 && (
            <ArenaRiskBadge label={`${scoreboard.invariantFailureCount} invariant failures`} />
          )}
          {scoreboard.rejectedWinnerEventCount > 0 && (
            <ArenaRiskBadge label={`${scoreboard.rejectedWinnerEventCount} rejected winners`} />
          )}
        </div>
      )}
    </SurfacePanel>
  );
}

function ArenaParticipantRow({
  participant,
}: {
  participant: ArenaScoreboardParticipant;
}) {
  const hasIssue = participant.exclusions > 0 || participant.losses > 0;
  const reason = Object.entries(participant.exclusionReasons)[0];
  return (
    <SurfacePanel as="article" className="bg-runtime-bg/60 p-2">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="break-all font-mono text-[11px] text-ink-soft">
          {participant.participantId}
        </span>
        <EvidenceBadge tone={hasIssue ? "amber" : "emerald"} className="rounded-md text-[10px]">
          {participant.wins}W {participant.losses}L
        </EvidenceBadge>
      </div>
      <div className="mt-2 flex items-center gap-1.5 overflow-hidden text-[10px]">
        <KernelTraceSegment
          label="score"
          value={formatArenaNumber(participant.totalScore)}
          tone="cyan"
        />
        <KernelTraceRail />
        <KernelTraceSegment
          label="cost"
          value={formatArenaNumber(participant.totalCost)}
          tone="neutral"
        />
        <KernelTraceRail />
        <KernelTraceSegment
          label="excl"
          value={participant.exclusions}
          tone={participant.exclusions ? "red" : "emerald"}
        />
      </div>
      {reason && (
        <div className="mt-2 truncate text-[10px] text-signal-authority">
          {reason[0]} x{reason[1]}
        </div>
      )}
    </SurfacePanel>
  );
}

function ArenaRiskBadge({ label }: { label: string }) {
  return (
    <EvidenceBadge tone="red" className="rounded-md text-[10px]">
      {label}
    </EvidenceBadge>
  );
}

function formatArenaNumber(value: number): string | number {
  if (!Number.isFinite(value)) return "-";
  if (Math.abs(value) >= 100) return Math.round(value);
  if (Number.isInteger(value)) return value;
  return value.toFixed(2);
}
