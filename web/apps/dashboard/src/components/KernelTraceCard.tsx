import { lazy, Suspense } from "react";
import type { KernelTraceView } from "../kernelTrace";
import { SurfacePanel } from "./DashboardChrome";

// Re-export the dependency-light primitives statically so evidence cards
// (ChatEvidenceCards) keep their existing import surface — `import { ..., Kernel*
// } from "./KernelTraceCard"` — without pulling the heavy trace body into the
// initial bundle.
export {
  KernelTraceStat,
  KernelTraceGraphNode,
  KernelTraceSegment,
  KernelTraceRail,
} from "./kernelTracePrimitives";

// The heavy trace explorer body is code-split: it is only fetched when a card is
// actually rendered. Module identity is stable (single lazy() created at module
// load), so repeated mounts reuse the same resolved component.
const KernelTraceCardBody = lazy(() => import("./KernelTraceCardBody"));

function KernelTraceSkeleton({ compact = false }: { compact?: boolean }) {
  return (
    <SurfacePanel
      as="section"
      role="status"
      aria-label="Loading kernel trace"
      aria-busy="true"
      className={`mt-3 animate-pulse space-y-3 bg-runtime-bg p-3 ${compact ? "text-xs" : ""}`}
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

export function KernelTraceCard({
  trace,
  compact = false,
}: {
  trace: KernelTraceView;
  compact?: boolean;
}) {
  return (
    <Suspense fallback={<KernelTraceSkeleton compact={compact} />}>
      <KernelTraceCardBody trace={trace} compact={compact} />
    </Suspense>
  );
}
