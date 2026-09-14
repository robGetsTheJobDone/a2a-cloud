import type { ControlPolicy, ControlSummary, ControlTimelineItem } from "../../api";
import { buildControlFailureReviewQueue, type TimelineSummary } from "./controlData";
import { PolicySnapshot } from "./ControlPanels";
import { ControlActivitySummary, DagRunsPanel } from "./ControlTimeline";
import { ControlFailureReviewPanel } from "./FailureReviewQueue";

export function ControlOverviewView({
  summary,
  liveDagRuns,
  recentDagRuns,
  timeline,
  activity,
  policy,
  dirty,
  onReceipt,
}: {
  summary: ControlSummary;
  liveDagRuns: ControlTimelineItem[];
  recentDagRuns: ControlTimelineItem[];
  timeline: ControlTimelineItem[];
  activity: TimelineSummary;
  policy: ControlPolicy | null;
  dirty: boolean;
  onReceipt: (item: ControlTimelineItem) => void;
}) {
  const failureQueue = buildControlFailureReviewQueue(timeline, summary.failures);

  return (
    <>
      <div className="grid gap-5 xl:grid-cols-[minmax(280px,0.8fr)_minmax(0,1.2fr)]">
        {policy && (
          <div className="space-y-5">
            <PolicySnapshot
              policy={policy}
              summary={summary}
              dirty={dirty}
            />
          </div>
        )}

        <div className={policy ? "space-y-5" : "space-y-5 xl:col-span-2"}>
          <ControlFailureReviewPanel
            queue={failureQueue}
            onReceipt={onReceipt}
          />
          <DagRunsPanel
            liveDagRuns={liveDagRuns}
            recentDagRuns={recentDagRuns}
            onReceipt={onReceipt}
          />
          <ControlActivitySummary
            filteredCount={timeline.length}
            sourceName="All sources"
            activity={activity}
            onReceipt={onReceipt}
          />
        </div>
      </div>
    </>
  );
}
