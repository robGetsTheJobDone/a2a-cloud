import { useEffect, useState } from "react";
import {
  InlineAlert,
  ToolbarButton,
} from "./DashboardChrome";
import { StatusBadge } from "./StatusPillAdapters";
import {
  browserAnchorScheduler,
  clickAdvancesTour,
  findVisibleTourTarget,
  measureTourElement,
  paddedRect,
  popoverStyle,
  prefersReducedMotion,
  tourSpotlightNote,
  watchTourAnchor,
  type SpotlightState,
  type TourRoute,
  type TourStep,
} from "./onboarding/tourSteps";

export type { TourRoute } from "./onboarding/tourSteps";

/**
 * What the spotlight is currently attached to. `pending` is the window between
 * navigating and the target mounting — it renders no ring and no apology,
 * because nothing has gone wrong yet.
 */
type SpotlightAnchor = {
  kind: SpotlightState;
  rect: DOMRect | null;
};

const PENDING: SpotlightAnchor = { kind: "pending", rect: null };

export type GuidedTourProps = {
  activeTour: TourStep;
  busy: boolean;
  error: string | null;
  index: number;
  total: number;
  onBack: () => void;
  onComplete: () => void;
  onNavigate: (route: TourRoute) => void;
  onNext: () => void;
};

/**
 * GuidedTour — the spotlight + popover overlay driven by the onboarding wizard.
 *
 * Positioning is class-based (Tailwind utilities for the chrome) with only the
 * spotlight box + popover anchor computed inline from the live target rect.
 * Honours prefers-reduced-motion (no spotlight transition / smooth scroll),
 * spotlights with the signal.protocol mint accent, and gracefully centres the
 * popover when a step's target is missing from the DOM.
 */
export function GuidedTour({
  activeTour,
  busy,
  error,
  index,
  total,
  onBack,
  onComplete,
  onNavigate,
  onNext,
}: GuidedTourProps) {
  const [anchor, setAnchor] = useState<SpotlightAnchor>(PENDING);
  const reducedMotion = prefersReducedMotion();

  useEffect(() => {
    setAnchor(PENDING);
    onNavigate(activeTour.route);
    // The route change lands in a later commit than this effect, so wait for
    // the node instead of measuring once at a fixed delay — measuring once
    // meant whichever element happened to be on screen at that instant won the
    // spotlight for the whole step. The watch stays alive for the rest of the
    // step: a target that mounts late (Studio's "Build new anyway", which only
    // exists once the reuse check has run) still takes the ring, and one that
    // unmounts gives it back.
    return watchTourAnchor(
      () => findVisibleTourTarget(activeTour),
      ({ kind, element }) =>
        setAnchor({
          kind,
          rect: element ? measureTourElement(element) : null,
        }),
      browserAnchorScheduler(),
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeTour.route, activeTour.target]);

  useEffect(() => {
    function update() {
      // Scroll/resize only moves an anchor that already exists. Whether it
      // exists at all is the watch's job, so a stale rect here never turns into
      // a claim about the control being gone.
      setAnchor((prev) => {
        if (prev.kind !== "target") return prev;
        const element = findVisibleTourTarget(activeTour);
        if (!element) return prev;
        return { kind: "target", rect: element.getBoundingClientRect() };
      });
    }
    window.addEventListener("resize", update);
    window.addEventListener("scroll", update, true);
    return () => {
      window.removeEventListener("resize", update);
      window.removeEventListener("scroll", update, true);
    };
  }, [activeTour]);

  useEffect(() => {
    function onDocumentClick(event: MouseEvent) {
      if (busy) return;
      const target = event.target instanceof Element ? event.target : null;
      if (!clickAdvancesTour(activeTour, target)) return;
      window.setTimeout(onNext, 0);
    }

    document.addEventListener("click", onDocumentClick, true);
    return () => document.removeEventListener("click", onDocumentClick, true);
  }, [activeTour, busy, onNext]);

  const spotlight = anchor.rect ? paddedRect(anchor.rect, 8) : null;
  const popover = popoverStyle(spotlight);
  const isLast = index === total - 1;
  const note = tourSpotlightNote(activeTour, anchor.kind);

  return (
    <>
      {spotlight && (
        <div
          className={
            "pointer-events-none fixed z-50 rounded-lg ring-2 ring-signal-protocol ring-offset-2 ring-offset-runtime-bg shadow-[0_0_24px_rgba(52,211,153,0.4)]" +
            (reducedMotion ? "" : " transition-all duration-200 ease-out")
          }
          style={{
            top: spotlight.top,
            left: spotlight.left,
            width: spotlight.width,
            height: spotlight.height,
          }}
        />
      )}

      <aside
        role="dialog"
        aria-live="polite"
        aria-label="Dashboard walkthrough hint"
        className="fixed z-50 rounded-xl border border-signal-protocol/40 bg-runtime-panel p-4 shadow-2xl shadow-black/45 ring-1 ring-signal-protocol/10"
        style={popover}
      >
        <div className="flex items-center justify-between gap-3">
          <StatusBadge tone="emerald">
            {index + 1}/{total}
          </StatusBadge>
        </div>
        <h2 className="mt-3 text-base font-semibold text-ink">
          {activeTour.label}
        </h2>
        <p className="mt-1 text-sm leading-relaxed text-ink-dim">
          {activeTour.summary}
        </p>
        {note && (
          <p className="mt-2 text-[11px] text-ink-muted">{note}</p>
        )}
        {error && (
          <div className="mt-3">
            <InlineAlert tone="red">{error}</InlineAlert>
          </div>
        )}
        {/* "End tour" sits in the same row, at the same weight, as Next. Leaving
            is a normal choice here, not an escape hatch tucked into a corner. */}
        <div className="mt-3 flex flex-wrap items-center justify-between gap-2">
          <ToolbarButton onClick={onBack} disabled={busy || index === 0}>
            Back
          </ToolbarButton>
          <div className="flex gap-2">
            <ToolbarButton type="button" onClick={onComplete} disabled={busy}>
              {busy ? "Ending..." : "End tour"}
            </ToolbarButton>
            {isLast ? (
              <ToolbarButton variant="primary" onClick={onComplete} disabled={busy}>
                {busy ? "Finishing..." : "Finish"}
              </ToolbarButton>
            ) : (
              <ToolbarButton variant="primary" onClick={onNext} disabled={busy}>
                Next
              </ToolbarButton>
            )}
          </div>
        </div>
      </aside>
    </>
  );
}
