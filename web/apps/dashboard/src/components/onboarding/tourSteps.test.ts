import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  TOUR_ANCHOR_CEILING_MS,
  TOUR_ANCHOR_RECHECK_MS,
  TOUR_STEPS,
  browserAnchorScheduler,
  findVisibleTourTarget,
  tourSpotlightNote,
  watchTourAnchor,
  type AnchorScheduler,
  type TourAnchor,
  type TourStep,
} from "./tourSteps";

/**
 * The anchor watch is the whole fix for the cold-load spotlight bug, and it is
 * pure apart from two injected seams (the DOM lookup and the scheduler), so it
 * is tested here directly. The dashboard suite runs in Node with no DOM, so
 * both seams are hand-rolled fakes and time is stepped by hand.
 */

/** A scheduler the test drives by hand, one queued callback batch per `tick`. */
function fakeScheduler(frameMs = 16) {
  let elapsed = 0;
  const pending: Array<{ callback: () => void; delayMs: number }> = [];

  const scheduler: AnchorScheduler = {
    schedule(callback, delayMs) {
      const entry = { callback, delayMs };
      pending.push(entry);
      return () => {
        const at = pending.indexOf(entry);
        if (at >= 0) pending.splice(at, 1);
      };
    },
    elapsedMs: () => elapsed,
  };

  return {
    scheduler,
    get pendingCount() {
      return pending.length;
    },
    /** Delay each queued callback asked for (frame delays count as `frameMs`). */
    get lastDelayMs() {
      return pending.length ? pending[pending.length - 1].delayMs : null;
    },
    /** Run every queued callback, advancing the clock by what they asked for. */
    tick() {
      const queued = pending.splice(0, pending.length);
      elapsed += Math.max(
        frameMs,
        ...queued.map((entry) => entry.delayMs || frameMs),
      );
      for (const entry of queued) entry.callback();
    },
  };
}

/** A DOM stand-in whose target presence the test flips at will. */
function fakeTarget(name = "target") {
  return { name } as unknown as HTMLElement;
}

function collect() {
  const seen: TourAnchor[] = [];
  return { seen, onAnchor: (anchor: TourAnchor) => void seen.push(anchor) };
}

const step = (overrides: Partial<TourStep> = {}): TourStep => ({
  route: "studio",
  target: "studio-build",
  label: "Start the build",
  summary: "summary",
  ...overrides,
});

describe("watchTourAnchor", () => {
  it("anchors synchronously when the target is already mounted", () => {
    const element = fakeTarget();
    const { seen, onAnchor } = collect();
    const frames = fakeScheduler();

    watchTourAnchor(() => element, onAnchor, frames.scheduler);

    expect(seen).toEqual([{ kind: "target", element }]);
  });

  it("anchors on the frame the target appears, not a fixed delay later", () => {
    // The bug this replaces: one measurement at a fixed delay, so a page that
    // rendered a few frames later never got a spotlight at all.
    const element = fakeTarget();
    let mounted = false;
    const { seen, onAnchor } = collect();
    const frames = fakeScheduler();

    watchTourAnchor(() => (mounted ? element : null), onAnchor, frames.scheduler);
    expect(seen).toEqual([]);

    frames.tick();
    frames.tick();
    expect(seen).toEqual([]);
    // Still racing the mount, so it is still polling every frame.
    expect(frames.lastDelayMs).toBe(0);

    mounted = true;
    frames.tick();
    expect(seen).toEqual([{ kind: "target", element }]);
  });

  it("says nothing about a missing control until the ceiling", () => {
    const { seen, onAnchor } = collect();
    const frames = fakeScheduler();

    watchTourAnchor(() => null, onAnchor, frames.scheduler, { ceilingMs: 200 });
    for (let i = 0; i < 12; i += 1) {
      // 12 frames * 16ms = 192ms, still inside the grace period.
      expect(seen).toEqual([]);
      frames.tick();
    }
    expect(seen).toEqual([]);

    frames.tick(); // 208ms
    expect(seen).toEqual([{ kind: "missing", element: null }]);
  });

  it("promotes to the real target when it mounts after the ceiling", () => {
    // Studio's "Build new anyway" only exists once the reuse check has run, so
    // it can appear long after the step opened. A one-shot resolve left the
    // popover insisting the control was off screen while it sat there.
    const element = fakeTarget("build-new-anyway");
    let revealed = false;
    const { seen, onAnchor } = collect();
    const frames = fakeScheduler();

    watchTourAnchor(() => (revealed ? element : null), onAnchor, frames.scheduler, {
      ceilingMs: 30,
      recheckMs: 250,
    });
    frames.tick(); // 16ms
    frames.tick(); // 32ms, past the ceiling
    expect(seen).toEqual([{ kind: "missing", element: null }]);

    // Minutes of the user reading, then acting.
    for (let i = 0; i < 20; i += 1) frames.tick();
    expect(seen.length).toBe(1);

    revealed = true;
    frames.tick();
    expect(seen).toEqual([
      { kind: "missing", element: null },
      { kind: "target", element },
    ]);
  });

  it("backs off to the slow recheck once it has an answer", () => {
    // Watching for the rest of the step must not mean a querySelectorAll every
    // frame for as long as the popover is open.
    const { onAnchor } = collect();
    const frames = fakeScheduler();

    watchTourAnchor(() => null, onAnchor, frames.scheduler, {
      ceilingMs: 30,
      recheckMs: 250,
    });
    expect(frames.lastDelayMs).toBe(0);

    frames.tick();
    frames.tick();
    expect(frames.lastDelayMs).toBe(250);
  });

  it("gives the ring back when the anchored node unmounts", () => {
    const element = fakeTarget();
    let mounted = true;
    const { seen, onAnchor } = collect();
    const frames = fakeScheduler();

    watchTourAnchor(() => (mounted ? element : null), onAnchor, frames.scheduler);
    expect(seen).toEqual([{ kind: "target", element }]);

    mounted = false;
    frames.tick();
    expect(seen).toEqual([
      { kind: "target", element },
      { kind: "missing", element: null },
    ]);
  });

  it("reports changes only, so the callback can scroll the anchor into view", () => {
    const element = fakeTarget();
    const { seen, onAnchor } = collect();
    const frames = fakeScheduler();

    watchTourAnchor(() => element, onAnchor, frames.scheduler);
    for (let i = 0; i < 30; i += 1) frames.tick();

    expect(seen).toEqual([{ kind: "target", element }]);
  });

  it("stops polling when the callback itself cancels the watch", () => {
    // GuidedTour's callback is a setState, which can unmount the overlay and
    // run the canceller before `attempt` finishes.
    const element = fakeTarget();
    let mounted = false;
    const frames = fakeScheduler();
    let calls = 0;

    const cancel = watchTourAnchor(
      () => (mounted ? element : null),
      () => {
        calls += 1;
        cancel();
      },
      frames.scheduler,
    );

    mounted = true;
    frames.tick();

    expect(calls).toBe(1);
    expect(frames.pendingCount).toBe(0);
  });

  it("never reports after cancel, and stops polling", () => {
    let mounted = false;
    const { seen, onAnchor } = collect();
    const frames = fakeScheduler();

    const cancel = watchTourAnchor(
      () => (mounted ? fakeTarget() : null),
      onAnchor,
      frames.scheduler,
      { ceilingMs: 10 },
    );
    cancel();
    mounted = true;
    for (let i = 0; i < 10; i += 1) frames.tick();

    expect(seen).toEqual([]);
    expect(frames.pendingCount).toBe(0);
  });

  it("keeps a ceiling a stalled step can actually reach, and a live recheck", () => {
    expect(TOUR_ANCHOR_CEILING_MS).toBeGreaterThan(0);
    expect(TOUR_ANCHOR_CEILING_MS).toBeLessThanOrEqual(5000);
    expect(TOUR_ANCHOR_RECHECK_MS).toBeGreaterThan(0);
    expect(TOUR_ANCHOR_RECHECK_MS).toBeLessThanOrEqual(1000);
  });
});

// The unit tests above inject both seams. This one runs the REAL scheduler and
// the REAL DOM lookup against a fake document, on the walkthrough's own worst
// step, so the wiring between them is covered too.
describe("Studio's build step, end to end on the production scheduler", () => {
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  function fakeNode(name: string) {
    return {
      name,
      getBoundingClientRect: () => ({ width: 180, height: 32 }),
    } as unknown as HTMLElement;
  }

  it("waits, says how to reveal the control, then takes the ring when it appears", () => {
    vi.useFakeTimers({
      toFake: [
        "setTimeout",
        "clearTimeout",
        "requestAnimationFrame",
        "cancelAnimationFrame",
        "performance",
        "Date",
      ],
    });

    const buildStep = TOUR_STEPS.find((step) => step.target === "studio-build");
    if (!buildStep) throw new Error("the walkthrough no longer has a build step");

    const sidebarLink = fakeNode("sidebar:Studio");
    const buildButton = fakeNode("Build new anyway");
    // A cold /studio: the brief is up, the reuse panel (and its build button) is
    // not, and the sidebar nav item for the route the user is already on is —
    // as always — right there to be mis-anchored to.
    const nodes: Record<string, HTMLElement> = {
      '[data-onboarding-target="studio-brief"]': fakeNode("goal field"),
      '[data-onboarding-route="studio"]': sidebarLink,
    };
    vi.stubGlobal("document", {
      querySelectorAll: (selector: string) =>
        nodes[selector] ? [nodes[selector]] : [],
    });
    vi.stubGlobal("window", {
      getComputedStyle: () => ({ visibility: "visible", display: "block" }),
    });

    const { seen, onAnchor } = collect();
    const stop = watchTourAnchor(
      () => findVisibleTourTarget(buildStep),
      onAnchor,
      browserAnchorScheduler(),
    );

    // Most of the grace period: no ring, and nothing said.
    vi.advanceTimersByTime(TOUR_ANCHOR_CEILING_MS - 200);
    expect(seen).toEqual([]);
    expect(tourSpotlightNote(buildStep, "pending")).toBeNull();

    // Past it: the step admits the control is not there and says what brings it.
    vi.advanceTimersByTime(400);
    expect(seen).toEqual([{ kind: "missing", element: null }]);
    expect(tourSpotlightNote(buildStep, "missing")).toBe(buildStep.revealedBy);

    // The user reads for a while, then runs the reuse check.
    vi.advanceTimersByTime(30_000);
    expect(seen.length).toBe(1);
    nodes['[data-onboarding-target="studio-build"]'] = buildButton;
    vi.advanceTimersByTime(TOUR_ANCHOR_RECHECK_MS * 2);

    expect(seen).toEqual([
      { kind: "missing", element: null },
      { kind: "target", element: buildButton },
    ]);
    expect(tourSpotlightNote(buildStep, "target")).toBeNull();
    // At no point did the ring land on the nav item for the page the user was
    // already looking at.
    expect(seen.some((anchor) => anchor.element === sidebarLink)).toBe(false);

    stop();
  });
});

describe("the spotlight never anchors to a sidebar nav item", () => {
  // GuidedTour navigates to the step's route BEFORE it starts looking, so any
  // "fall back to the route's nav item" rule could only ever ring the nav item
  // for the page the user is already on: a confident mint ring on an unrelated
  // control, carrying no information, that does not even advance the tour when
  // clicked. Structural guard so it cannot creep back in.
  for (const module of ["./tourSteps.ts", "../GuidedTour.tsx"]) {
    it(`${module} cannot select one`, () => {
      const source = readFileSync(
        fileURLToPath(new URL(module, import.meta.url)),
        "utf8",
      );

      expect(
        source.includes("data-onboarding-route"),
        `${module} selects the sidebar nav item for a route. The tour has ` +
          "already navigated there, so that ring would land on the current " +
          "page's own nav link.",
      ).toBe(false);
    });
  }
});

describe("tourSpotlightNote", () => {
  const plain = step({ target: "activity-list", revealedBy: undefined });

  it("says nothing while the page is still mounting or the ring is up", () => {
    expect(tourSpotlightNote(plain, "pending")).toBeNull();
    expect(tourSpotlightNote(plain, "target")).toBeNull();
  });

  it("names no button, because the last step's is Finish rather than Next", () => {
    const note = tourSpotlightNote(plain, "missing");

    expect(note).toBeTruthy();
    expect(note).not.toMatch(/\bNext\b|\bFinish\b|\bBack\b/);
  });

  it("tells the user how to reveal a conditionally rendered control", () => {
    const conditional = step({
      revealedBy: "Run the reuse check above and it lights up here.",
    });

    expect(tourSpotlightNote(conditional, "missing")).toBe(conditional.revealedBy);
    // ...and only when it is actually absent.
    expect(tourSpotlightNote(conditional, "pending")).toBeNull();
  });

  it("keeps the one conditional step's instruction pointed at what reveals it", () => {
    const conditional = TOUR_STEPS.filter((entry) => entry.revealedBy);

    // Exactly one target in the walkthrough is conditionally rendered.
    expect(conditional.map((entry) => entry.target)).toEqual(["studio-build"]);
    expect(conditional[0].revealedBy).toMatch(/reuse/i);
  });
});
