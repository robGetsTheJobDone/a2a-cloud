import { useMemo, useState } from "react";
import type { EvidenceTimeline, EvidenceTimelineLane } from "../../../api";
import { SegmentedControl, SurfacePanel, TabPill } from "../../DashboardChrome";
import { EVIDENCE_LANES, EvidenceTimelineRow } from "./evidenceShared";

export function EvidenceTimelineTab({
  timeline,
  hasError,
}: {
  timeline: EvidenceTimeline | null;
  hasError: boolean;
}) {
  const [lane, setLane] = useState<"all" | EvidenceTimelineLane>("all");
  const items = useMemo(() => {
    const all = timeline?.items || [];
    return lane === "all" ? all : all.filter((item) => item.lane === lane);
  }, [lane, timeline]);

  return (
    <>
      <SegmentedControl
        role="tablist"
        aria-label="Evidence lanes"
        className="mt-4 flex gap-1 overflow-x-auto bg-runtime-bg"
      >
        {EVIDENCE_LANES.map((nextLane) => (
          <TabPill
            key={nextLane}
            onClick={() => setLane(nextLane)}
            selected={lane === nextLane}
            className="shrink-0"
          >
            {nextLane}
          </TabPill>
        ))}
      </SegmentedControl>

      <div className="mt-3 grid gap-2">
        {items.slice(0, 18).map((item) => (
          <EvidenceTimelineRow key={item.id} item={item} />
        ))}
        {timeline && items.length === 0 && (
          <SurfacePanel as="div" className="p-3 text-xs text-ink-muted">
            No evidence rows for this lane yet.
          </SurfacePanel>
        )}
        {!timeline && !hasError && (
          <SurfacePanel as="div" className="p-3 text-xs text-ink-muted">
            Loading evidence...
          </SurfacePanel>
        )}
      </div>
    </>
  );
}
