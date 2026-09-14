import type { KernelTraceView } from "../kernelTrace";
import {
  SurfacePanel,
} from "./DashboardChrome";
import { StatusBadge } from "./StatusPillAdapters";
import {
  KernelTraceBadge,
  KernelTraceGraphNode,
  KernelTraceRail,
  KernelTraceSegment,
  KernelTraceStat,
} from "./kernelTracePrimitives";

// Heavy kernel-trace render body. Imported lazily by KernelTraceCard so that
// the deep trace explorer (timelines, policy lists, per-lane breakdowns) is only
// pulled into the bundle when a trace is actually shown.
export default function KernelTraceCardBody({
  trace,
  compact = false,
}: {
  trace: KernelTraceView;
  compact?: boolean;
}) {
  const invariantTotal = trace.invariantPassCount + trace.invariantFailCount;
  const traceTone = trace.passed ? "emerald" : "red";
  return (
    <SurfacePanel
      as="section"
      className={`mt-3 bg-runtime-bg p-3 ${compact ? "text-xs" : ""}`}
      aria-label="Kernel trace graph"
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <StatusBadge tone={traceTone} className="rounded-md font-semibold">
              {trace.passed ? "kernel passed" : "kernel attention"}
            </StatusBadge>
            {trace.protocolId && (
              <span className="break-all font-mono text-[11px] text-ink-muted">
                {trace.protocolId}
                {trace.protocolVersion ? `@${trace.protocolVersion}` : ""}
              </span>
            )}
            {trace.riskClass && (
              <StatusBadge tone="neutral" className="rounded-md text-[10px] uppercase">
                {trace.riskClass}
              </StatusBadge>
            )}
          </div>
          <div className="mt-2 text-sm font-semibold text-ink">
            {trace.title}
          </div>
        </div>
        <div className="grid grid-cols-3 gap-1.5 text-right sm:min-w-[220px]">
          <KernelTraceStat label="scenes" value={trace.scenarioCount || "-"} />
          <KernelTraceStat
            label="checks"
            value={invariantTotal || "-"}
            tone={trace.invariantFailCount ? "red" : "emerald"}
          />
          <KernelTraceStat label="replay" value={trace.replayPassCount || "-"} />
        </div>
      </div>

      <div className="mt-3 grid gap-2 sm:grid-cols-3">
        <KernelTraceGraphNode
          label="simulation"
          value={`${trace.scenarioCount || trace.totalTraceCount} scenarios`}
          tone={trace.passed ? "cyan" : "amber"}
        />
        <KernelTraceGraphNode
          label="invariants"
          value={`${trace.invariantPassCount}/${invariantTotal || 0} pass`}
          tone={trace.invariantFailCount ? "red" : "emerald"}
        />
        <KernelTraceGraphNode
          label="runtime gate"
          value={trace.violationCount ? `${trace.violationCount} blocked` : "clear"}
          tone={trace.violationCount ? "amber" : "emerald"}
        />
      </div>

      {trace.runtimeGate && (
        <SurfacePanel as="section" className="mt-3 bg-runtime-bg/60 p-2">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="text-[10px] uppercase text-ink-faint">
              runtime gate
            </div>
            <StatusBadge
              tone={
                trace.runtimeGate.allowed || trace.runtimeGate.activeApplyEnabled
                  ? "amber"
                  : "emerald"
              }
              className="rounded-md text-[10px]"
            >
              {trace.runtimeGate.activeApplyEnabled
                ? "active apply enabled"
                : trace.runtimeGate.allowed
                  ? "allowed"
                  : "simulation only"}
            </StatusBadge>
          </div>
          {trace.runtimeGate.reason && (
            <div className="mt-1 text-[11px] text-ink-dim">
              {trace.runtimeGate.reason}
            </div>
          )}
          <div className="mt-2 flex flex-wrap gap-1">
            {trace.runtimeGate.satisfiedGates.slice(0, 4).map((gate) => (
              <KernelTraceBadge key={`ok:${gate}`} label={gate} tone="emerald" />
            ))}
            {trace.runtimeGate.missingGates.slice(0, 4).map((gate) => (
              <KernelTraceBadge key={`missing:${gate}`} label={gate} tone="amber" />
            ))}
          </div>
        </SurfacePanel>
      )}

      {trace.policies.length > 0 && (
        <KernelTracePolicyList policies={trace.policies.slice(0, 5)} />
      )}

      {trace.traces.length > 0 && (
        <div className="mt-3 space-y-2">
          {trace.traces.map((lane) => (
            <SurfacePanel
              as="article"
              key={lane.scenarioId}
              className="grid gap-2 bg-runtime-bg/60 p-2 md:grid-cols-[minmax(0,1fr)_auto]"
            >
              <div className="min-w-0">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="break-all font-mono text-[11px] text-ink-soft">
                    {lane.scenarioId}
                  </span>
                  <StatusBadge
                    tone={lane.passed ? "emerald" : "red"}
                    className="rounded-md text-[10px]"
                  >
                    {lane.passed ? "pass" : "fail"}
                  </StatusBadge>
                  {lane.title && (
                    <span className="truncate text-[11px] text-ink-muted">
                      {lane.title}
                    </span>
                  )}
                </div>
                <div className="mt-2 flex items-center gap-1.5 overflow-hidden text-[10px]">
                  <KernelTraceSegment
                    label="events"
                    value={lane.eventCount}
                    tone="cyan"
                  />
                  <KernelTraceRail />
                  <KernelTraceSegment
                    label="checks"
                    value={`${lane.invariantPassCount}/${
                      lane.invariantCount ||
                      lane.invariantPassCount + lane.invariantFailCount
                    }`}
                    tone={lane.invariantFailCount ? "red" : "emerald"}
                  />
                  <KernelTraceRail />
                  <KernelTraceSegment
                    label="replay"
                    value={lane.replayPassed ? "ok" : "-"}
                    tone={lane.replayPassed ? "emerald" : "neutral"}
                  />
                </div>
              </div>
              {(lane.alerts.length > 0 || lane.violations.length > 0) && (
                <div className="flex max-w-full flex-wrap gap-1 md:max-w-[220px] md:justify-end">
                  {lane.alerts.slice(0, 2).map((alert) => (
                    <StatusBadge key={alert} tone="amber" className="rounded-md text-[10px]">
                      {alert}
                    </StatusBadge>
                  ))}
                  {lane.violations.slice(0, 2).map((violation) => (
                    <StatusBadge key={violation} tone="red" className="rounded-md text-[10px]">
                      {violation}
                    </StatusBadge>
                  ))}
                </div>
              )}
              {(lane.invariants.length > 0 ||
                lane.policies.length > 0 ||
                lane.timeline.length > 0) && (
                <div className="md:col-span-2">
                  {lane.invariants.length > 0 && (
                    <div className="mt-1 flex flex-wrap gap-1">
                      {lane.invariants.slice(0, 8).map((invariant) => (
                        <KernelTraceBadge
                          key={`${lane.scenarioId}:inv:${invariant.id}`}
                          label={invariant.id}
                          tone={invariant.passed ? "emerald" : "red"}
                        />
                      ))}
                    </div>
                  )}
                  {lane.policies.length > 0 && (
                    <KernelTracePolicyList policies={lane.policies.slice(0, 4)} compact />
                  )}
                  {lane.timeline.length > 0 && (
                    <div className="mt-2 grid gap-1.5">
                      {lane.timeline.map((event) => (
                        <KernelTraceTimelineRow
                          key={`${lane.scenarioId}:${
                            event.seq || event.eventType
                          }:${event.summary}`}
                          event={event}
                        />
                      ))}
                      {lane.omittedTimelineCount > 0 && (
                        <SurfacePanel as="div" className="bg-runtime-bg/50 px-2 py-1 text-[10px] text-ink-muted">
                          + {lane.omittedTimelineCount} more kernel events
                        </SurfacePanel>
                      )}
                    </div>
                  )}
                </div>
              )}
            </SurfacePanel>
          ))}
          {trace.omittedTraceCount > 0 && (
            <SurfacePanel as="div" className="bg-runtime-bg/60 px-2 py-1.5 text-[11px] text-ink-muted">
              + {trace.omittedTraceCount} more scenarios summarized
            </SurfacePanel>
          )}
        </div>
      )}

      {(trace.alerts.length > 0 || trace.violations.length > 0) && (
        <div className="mt-3 flex flex-wrap gap-1.5">
          {trace.alerts.slice(0, 4).map((alert) => (
            <StatusBadge key={alert} tone="amber" className="rounded-md text-[10px]">
              {alert}
            </StatusBadge>
          ))}
          {trace.violations.slice(0, 4).map((violation) => (
            <StatusBadge key={violation} tone="red" className="rounded-md text-[10px]">
              {violation}
            </StatusBadge>
          ))}
        </div>
      )}
    </SurfacePanel>
  );
}

function KernelTracePolicyList({
  policies,
  compact = false,
}: {
  policies: KernelTraceView["policies"];
  compact?: boolean;
}) {
  if (policies.length === 0) return null;
  return (
    <div className={`${compact ? "mt-2" : "mt-3"} grid gap-1.5`}>
      {policies.map((policy) => {
        const denied = policy.decision === "deny" || policy.effect === "deny";
        return (
          <SurfacePanel
            as="div"
            key={`${policy.id}:${policy.resource || ""}`}
            className="grid gap-2 bg-runtime-bg/60 px-2 py-1.5 text-[11px] sm:grid-cols-[auto_minmax(0,1fr)_auto]"
          >
            <StatusBadge
              tone={denied ? "red" : "emerald"}
              className="rounded-md text-[10px]"
            >
              {policy.decision}
            </StatusBadge>
            <div className="min-w-0">
              <div className="truncate font-mono text-ink-soft">
                {policy.resource || policy.id}
              </div>
              {policy.reason && !compact && (
                <div className="mt-0.5 truncate text-[10px] text-ink-muted">
                  {policy.reason}
                </div>
              )}
            </div>
            <div className="flex flex-wrap justify-start gap-1 sm:justify-end">
              {policy.signed && <KernelTraceBadge label="signed" tone="emerald" />}
              {policy.policyRefs.slice(0, 2).map((ref) => (
                <KernelTraceBadge key={ref} label={ref} tone="cyan" />
              ))}
            </div>
          </SurfacePanel>
        );
      })}
    </div>
  );
}

function KernelTraceTimelineRow({
  event,
}: {
  event: KernelTraceView["traces"][number]["timeline"][number];
}) {
  return (
    <SurfacePanel
      as="div"
      className="grid gap-2 bg-runtime-bg/50 px-2 py-1.5 text-[11px] sm:grid-cols-[auto_140px_minmax(0,1fr)]"
    >
      <span className="font-mono text-[10px] text-ink-faint">
        {event.seq ? `#${event.seq}` : "-"}
      </span>
      <KernelTraceBadge label={event.eventType} tone={event.tone} />
      <div className="min-w-0">
        <div className="truncate text-ink-soft">{event.summary}</div>
        {event.actor && (
          <div className="mt-0.5 truncate text-[10px] text-ink-faint">
            actor {event.actor}
          </div>
        )}
      </div>
    </SurfacePanel>
  );
}
