import type { CSSProperties } from "react";
import type { DashboardRouteId } from "../../navigation";

/**
 * Onboarding tour metadata + DOM targeting.
 *
 * Extracted from OnboardingWizard so the wizard, GuidedTour, and tests can
 * share a single source of truth for the walkthrough script and the rules for
 * locating + measuring a step's spotlight target in the live DOM.
 */

export type TourRoute = Extract<
  DashboardRouteId,
  | "workspace"
  | "trials"
  | "schedules"
  | "activity"
  | "runtime"
  | "compose"
  | "studio"
  | "my-agents"
  | "installed-agents"
  | "marketplace"
  | "access"
  | "keys"
>;

export type TourStep = {
  route: TourRoute;
  target: string;
  label: string;
  summary: string;
  /**
   * Whether clicking inside the spotlight advances the step. Default true.
   *
   * Set false for steps that spotlight a control the user has to *work* with:
   * advancing on click would move the tour (and, on a route change, unmount the
   * page) on the very click that starts the work — typing into a field or
   * kicking off a build.
   */
  advanceOnClick?: boolean;
  /**
   * Set ONLY on a step whose target is conditionally rendered, i.e. one that
   * genuinely is not on the page when the step opens. The string tells the user
   * what reveals it, and replaces the generic "not on screen" note.
   *
   * A step without this must anchor to a node its page renders in its default
   * state — pinned by "tour targets exist on the page each step navigates to",
   * which renders the real page and checks the markup.
   */
  revealedBy?: string;
};

/**
 * The onboarding walkthrough is a route to one outcome — a deployed agent with
 * a receipt — not a tour of the product surface. Studio is the shortest real
 * build path, so the walkthrough drops the user in it and then shows the two
 * places the result lands. Keep this list short; extra spotlights belong in the
 * pages themselves, not in a blocking overlay.
 *
 * Studio takes three steps because Studio really does take three actions:
 * describe it, check for something reusable, then build. Collapsing them would
 * make the walkthrough claim a build had started when it had not.
 */
export const TOUR_STEPS: TourStep[] = [
  {
    route: "studio",
    target: "studio-brief",
    label: "Describe the agent you want",
    summary:
      "One or two sentences is enough. This is the shortest path from an empty account to a deployed agent.",
    advanceOnClick: false,
  },
  {
    route: "studio",
    target: "studio-start",
    label: "Check for something reusable first",
    summary:
      "Studio looks for an agent you already have that fits the brief. Nothing is built yet — this only lists candidates.",
    advanceOnClick: false,
  },
  {
    route: "studio",
    target: "studio-build",
    label: "Start the build",
    summary:
      "\"Build new anyway\" hands the brief to the crew: it scaffolds, reviews, repairs what it finds, then deploys with a live URL and a signed proof receipt.",
    advanceOnClick: false,
    // Unlike every other target, this one is inside `{resolution && ...}` in
    // AgentStudio: it does not exist until the reuse check above has returned.
    // A user who clicks Next straight through gets this instead of a spotlight
    // on nothing, and the ring lands on the button the moment it appears.
    revealedBy:
      "\"Build new anyway\" appears with the reuse results — run the check above and it lights up here.",
  },
  {
    route: "my-agents",
    target: "my-agents-list",
    label: "Your agent lands here",
    summary:
      "Deployed agents, their public endpoints, runtime health, and proof receipts. Already run an A2A endpoint? Import it instead of building one.",
  },
  {
    route: "activity",
    target: "activity-list",
    label: "Every run leaves a receipt",
    summary:
      "The work ledger records each run with timing, spend, and the receipt you can replay or hand to a buyer.",
  },
];

export type SpotlightRect = {
  left: number;
  top: number;
  right: number;
  bottom: number;
  width: number;
  height: number;
};

/**
 * True when the user has asked the OS to reduce motion. The tour honours this
 * by skipping smooth scroll + spotlight transitions.
 */
export function prefersReducedMotion(): boolean {
  if (typeof window === "undefined" || !window.matchMedia) return false;
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

/** First node matching `selector` that actually occupies space on screen. */
function firstVisibleElement(selector: string): HTMLElement | null {
  if (typeof document === "undefined") return null;
  const elements = Array.from(document.querySelectorAll<HTMLElement>(selector));
  return (
    elements.find((element) => {
      const rect = element.getBoundingClientRect();
      const style = window.getComputedStyle(element);
      return (
        rect.width > 0 &&
        rect.height > 0 &&
        style.visibility !== "hidden" &&
        style.display !== "none"
      );
    }) ?? null
  );
}

/**
 * Locate the visible DOM node for a tour step: only an explicit
 * `data-onboarding-target`. A step anchors to its own control or to nothing.
 *
 * There is deliberately NO fallback to the sidebar nav item for the step's
 * route, and there must never be one. GuidedTour navigates to `step.route`
 * before it starts looking, so by the time any fallback could fire the user is
 * already ON that route — the "fallback" could only ever ring the nav item for
 * the page under their feet. That is a confident mint ring on an unrelated
 * control carrying zero information, and clicking it does not even advance the
 * tour. Centring the popover and saying so is strictly more honest.
 */
export function findVisibleTourTarget(step: TourStep): HTMLElement | null {
  return firstVisibleElement(`[data-onboarding-target="${step.target}"]`);
}

/** Scroll an anchor into view and measure it. */
export function measureTourElement(element: HTMLElement): DOMRect {
  element.scrollIntoView({
    block: "nearest",
    inline: "center",
    behavior: prefersReducedMotion() ? "auto" : "smooth",
  });
  return element.getBoundingClientRect();
}

/**
 * Grace period before a step admits its target is not on screen.
 *
 * GuidedTour navigates and then looks, so the target is absent for at least one
 * commit on every step; the page's own data fetch and render can take a few
 * frames more. This is the ceiling, not the expected wait — the watch reports
 * the target on the first frame it exists, synchronously when it is already
 * mounted. (Today no tour page is code-split: DashboardRoutes imports Studio,
 * My agents, and Activity statically and a test forbids lazy-loading them, so
 * the wait covers render latency, not chunk fetches.)
 */
export const TOUR_ANCHOR_CEILING_MS = 2000;

/**
 * Poll interval once the anchor has been reported at least once.
 *
 * The watch does not stop at the first answer: a conditionally rendered target
 * (see `TourStep.revealedBy`) can appear minutes into a step, and an anchored
 * node can be unmounted out from under the ring. Frame-rate polling is only
 * needed for the initial paint race; after that a few checks a second is
 * plenty and costs one querySelectorAll each.
 */
export const TOUR_ANCHOR_RECHECK_MS = 250;

type TourAnchorKind = "target" | "missing";

export type TourAnchor = {
  kind: TourAnchorKind;
  element: HTMLElement | null;
};

/**
 * Clock + callback source for the anchor watch. Injected so the resolution
 * logic can be tested in Node with no DOM and no real timers.
 *
 * `schedule` returns its own canceller rather than a handle, so the caller
 * never has to know whether a frame or a timer was used.
 */
export type AnchorScheduler = {
  /** Run `callback` later; `delayMs` 0 means "next animation frame". */
  schedule: (callback: () => void, delayMs: number) => () => void;
  /** Milliseconds since the watch started. */
  elapsedMs: () => number;
};

export type TourAnchorWatchOptions = {
  ceilingMs?: number;
  recheckMs?: number;
};

/**
 * Track a tour step's spotlight anchor for the whole life of the step.
 *
 * Two outcomes, and the step can move between them as the DOM changes:
 *
 *  - `target`  — the step's own node, reported on the first frame it exists
 *                (synchronously if it is already mounted).
 *  - `missing` — no such node. Reported only once `ceilingMs` has passed, so a
 *                page that is still mounting is never accused of missing its
 *                control.
 *
 * `onAnchor` fires on CHANGE only, never twice for the same state, so the
 * caller can scroll the anchor into view in the callback without re-scrolling
 * on every poll. Because the watch keeps running after the first answer, a
 * target that mounts late (the reuse-check panel in Studio) promotes the step
 * out of `missing`, and a target that unmounts demotes it instead of leaving a
 * ring floating over the space where the control used to be.
 *
 * Returns a cancel function. After cancelling, `onAnchor` is never called.
 */
export function watchTourAnchor(
  findTarget: () => HTMLElement | null,
  onAnchor: (anchor: TourAnchor) => void,
  scheduler: AnchorScheduler,
  options: TourAnchorWatchOptions = {},
): () => void {
  const ceilingMs = options.ceilingMs ?? TOUR_ANCHOR_CEILING_MS;
  const recheckMs = options.recheckMs ?? TOUR_ANCHOR_RECHECK_MS;
  let cancelPending: (() => void) | null = null;
  let stopped = false;
  // undefined = nothing reported yet (still inside the grace period),
  // null = reported `missing`, otherwise the element currently reported.
  let reported: HTMLElement | null | undefined = undefined;

  function attempt() {
    cancelPending = null;
    if (stopped) return;

    const target = findTarget();
    if (target) {
      if (target !== reported) {
        reported = target;
        onAnchor({ kind: "target", element: target });
      }
    } else if (reported === undefined) {
      // Say nothing while the page may still be mounting.
      if (scheduler.elapsedMs() >= ceilingMs) {
        reported = null;
        onAnchor({ kind: "missing", element: null });
      }
    } else if (reported !== null) {
      // We were spotlighting a node and it went away.
      reported = null;
      onAnchor({ kind: "missing", element: null });
    }

    // `onAnchor` runs a React state update, which can unmount the tour and call
    // the canceller before we get here; re-check rather than queue a callback
    // nothing will ever cancel.
    if (stopped) return;
    cancelPending = scheduler.schedule(
      attempt,
      reported === undefined ? 0 : recheckMs,
    );
  }

  attempt();

  return () => {
    stopped = true;
    if (cancelPending) cancelPending();
    cancelPending = null;
  };
}

/**
 * Real-browser scheduler: animation frames for the initial race (so a target
 * that mounts on the next commit is spotlighted on the next frame rather than
 * after a fixed delay), timers for the slower recheck. Falls back to a timer
 * where rAF is unavailable.
 */
export function browserAnchorScheduler(): AnchorScheduler {
  const clock = () =>
    typeof performance !== "undefined" && typeof performance.now === "function"
      ? performance.now()
      : Date.now();
  const startedAt = clock();
  const hasRaf =
    typeof requestAnimationFrame === "function" &&
    typeof cancelAnimationFrame === "function";

  return {
    schedule(callback, delayMs) {
      if (delayMs <= 0 && hasRaf) {
        const frame = requestAnimationFrame(() => callback());
        return () => cancelAnimationFrame(frame);
      }
      const timer = setTimeout(callback, delayMs <= 0 ? 16 : delayMs);
      return () => clearTimeout(timer);
    },
    elapsedMs: () => clock() - startedAt,
  };
}

/** What the spotlight is attached to, from the popover's point of view. */
export type SpotlightState = "pending" | TourAnchorKind;

/**
 * The small print under a step's summary, or null when there is nothing to say.
 *
 * `pending` says nothing: the tour has only just navigated and nothing has gone
 * wrong yet. `missing` is the only state that earns a note, and a step whose
 * control is conditionally rendered supplies its own — a generic apology would
 * leave the user with no way to reveal the control the summary describes.
 *
 * Deliberately names no button: the last step's primary action is "Finish", not
 * "Next", so telling the user to "use Next" would name a control that is not
 * there.
 */
export function tourSpotlightNote(
  step: TourStep,
  state: SpotlightState,
): string | null {
  if (state !== "missing") return null;
  return (
    step.revealedBy ??
    "This control is not on screen right now — continue when you are ready."
  );
}

/**
 * Whether a click on `clicked` should advance `step`.
 *
 * Only a click inside the step's own spotlight counts, and only when the step
 * opts into click-advance. Clicking the sidebar link for the step's route used
 * to count too, which let a user skip a step by clicking the very nav item the
 * missing-target fallback had highlighted.
 */
export function clickAdvancesTour(
  step: TourStep,
  clicked: Element | null,
): boolean {
  if (step.advanceOnClick === false) return false;
  return Boolean(
    clicked?.closest(`[data-onboarding-target="${step.target}"]`),
  );
}

export function paddedRect(rect: DOMRect, padding: number): SpotlightRect {
  const left = Math.max(8, rect.left - padding);
  const top = Math.max(8, rect.top - padding);
  const right = Math.min(window.innerWidth - 8, rect.right + padding);
  const bottom = Math.min(window.innerHeight - 8, rect.bottom + padding);
  return {
    left,
    top,
    right,
    bottom,
    width: Math.max(0, right - left),
    height: Math.max(0, bottom - top),
  };
}

export function popoverStyle(
  spotlight: SpotlightRect | null,
): CSSProperties {
  const width = Math.min(360, window.innerWidth - 32);
  if (!spotlight) {
    return {
      width,
      left: Math.max(16, window.innerWidth - width - 24),
      top: 76,
    };
  }
  const below = spotlight.bottom + 14;
  const estimatedHeight = 260;
  const top =
    below + estimatedHeight < window.innerHeight
      ? below
      : Math.max(16, spotlight.top - estimatedHeight - 14);
  const left = Math.min(
    Math.max(16, spotlight.left),
    Math.max(16, window.innerWidth - width - 16),
  );
  return { width, left, top };
}
