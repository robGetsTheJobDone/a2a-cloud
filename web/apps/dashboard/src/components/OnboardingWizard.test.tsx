import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { renderToStaticMarkup } from "react-dom/server";
import { Route, Routes } from "react-router-dom";
import { StaticRouter } from "react-router-dom/server";
import { describe, expect, it, vi } from "vitest";
import type { OnboardingState } from "../api";
import { Activity } from "./Activity";
import { AgentStudio } from "./AgentStudio";
import { MyAgents } from "./MyAgents";
import { NoAgentsYetState } from "./AgentLifecycleEmptyStates";
import { DashboardSectionCacheProvider } from "./DashboardSectionCache";
import { GuidedTour } from "./GuidedTour";
import {
  OnboardingWizard,
  closeGestureForDraft,
  skipWasRecorded,
} from "./OnboardingWizard";
import {
  TOUR_STEPS,
  clickAdvancesTour,
  findVisibleTourTarget,
} from "./onboarding/tourSteps";

function onboardingState(overrides: Partial<OnboardingState> = {}): OnboardingState {
  return {
    completed: false,
    dismissed: false,
    current_step: "llm_key",
    llm_key_configured: false,
    llm_key_step_completed: false,
    walkthrough_completed: false,
    started_at: null,
    llm_key_completed_at: null,
    walkthrough_started_at: null,
    walkthrough_completed_at: null,
    last_seen_step: null,
    tour_step_index: 0,
    tour_step_total: 0,
    dismissed_at: null,
    completed_at: null,
    updated_at: null,
    ...overrides,
  };
}

/** class attribute of the <button> whose visible label is exactly `label`. */
function buttonClassFor(html: string, label: string): string {
  const at = html.indexOf(`>${label}<`);
  if (at < 0) throw new Error(`no button labelled "${label}" in markup`);
  const open = html.lastIndexOf("<button", at);
  const match = /class="([^"]*)"/.exec(html.slice(open, at));
  if (!match) throw new Error(`button "${label}" has no class attribute`);
  return match[1];
}

/** A stand-in for event.target that only matches `selector` via closest(). */
function clickedInside(selector: string): Element {
  return {
    closest: (candidate: string) => (candidate === selector ? {} : null),
  } as unknown as Element;
}

function renderWizard(state: OnboardingState) {
  return renderToStaticMarkup(
    <StaticRouter location="/workspace">
      <OnboardingWizard
        state={state}
        onStateChange={() => undefined}
        onComplete={() => undefined}
        onNavigate={() => undefined}
      />
    </StaticRouter>,
  );
}

describe("OnboardingWizard BYOK step", () => {
  it("offers a skip path instead of walling a new signup behind a provider key", () => {
    const html = renderWizard(onboardingState());

    // A real button, at the same weight as the save action — not just prose.
    expect(buttonClassFor(html, "Skip setup — build an agent"))
      .toContain("h-8 px-2.5 text-xs");
    // Dialog only renders its close control (and only binds Escape) when an
    // onClose handler is supplied, so this is the dismissibility signal.
    expect(html).toContain('aria-label="Close dialog"');
  });

  it("stays closed once the key step was skipped, without claiming completion", () => {
    const state = onboardingState({ dismissed: true, dismissed_at: "2026-08-01T00:00:00Z" });

    expect(state.completed).toBe(false);
    expect(renderWizard(state)).toBe("");
  });

  it("does not promise a second step the skip path never reaches", () => {
    const html = renderWizard(onboardingState());

    // The dialog has two outcomes (save a key, or skip to Studio), so it must
    // not advertise a fixed "Step 1 of 2 / Next: Walkthrough" march.
    expect(html).not.toContain("Step 1 of 2");
    expect(html).not.toContain("Walkthrough");
    expect(html).toContain("Studio");
  });
});

describe("setup dialog close gestures", () => {
  it("goes inert rather than discarding a key the user is part-way through", () => {
    expect(closeGestureForDraft("sk-live-abc")).toBe("keep-open");
    expect(closeGestureForDraft("   ")).toBe("hide");
    expect(closeGestureForDraft("")).toBe("hide");
  });

  it("refuses to act on a skip an older control plane silently dropped", () => {
    expect(skipWasRecorded(onboardingState({ dismissed: true }))).toBe(true);
    expect(skipWasRecorded(onboardingState({ dismissed: false }))).toBe(false);
    // Pre-`dismissed` server: 200 with the field absent entirely.
    const { dismissed: _omitted, ...legacy } = onboardingState();
    expect(skipWasRecorded(legacy as OnboardingState)).toBe(false);
  });
});

describe("onboarding walkthrough", () => {
  it("is a short route to a real outcome, not a tour of every admin screen", () => {
    expect(TOUR_STEPS.length).toBeLessThanOrEqual(5);
    expect(new Set(TOUR_STEPS.map((step) => step.route)).size).toBeLessThanOrEqual(3);
  });

  it("starts in Studio and ends on the receipt ledger", () => {
    expect(TOUR_STEPS[0].route).toBe("studio");
    expect(TOUR_STEPS[TOUR_STEPS.length - 1].route).toBe("activity");
  });

  it("drops the steps whose spotlight targets no longer exist", () => {
    const targets = TOUR_STEPS.map((step) => step.target);

    expect(targets).not.toContain("control-policy");
    expect(targets).not.toContain("control-timeline");
  });

  it("only claims the build has started on the step that starts it", () => {
    const startIndex = TOUR_STEPS.findIndex((s) => s.target === "studio-start");
    const buildIndex = TOUR_STEPS.findIndex((s) => s.target === "studio-build");

    expect(startIndex).toBeGreaterThanOrEqual(0);
    // The reuse check runs first and builds nothing, so its copy must not
    // promise a deploy or a receipt.
    const reuse = `${TOUR_STEPS[startIndex].label} ${TOUR_STEPS[startIndex].summary}`;
    expect(reuse).not.toMatch(/deploy|receipt|scaffold/i);
    // ...and the step that does promise those is the next one, anchored to the
    // "Build new anyway" control.
    expect(buildIndex).toBe(startIndex + 1);
    expect(TOUR_STEPS[buildIndex].route).toBe("studio");
    expect(TOUR_STEPS[buildIndex].summary).toMatch(/receipt/i);
  });

  it("never advances off Studio on the click that does the work", () => {
    const studioSteps = TOUR_STEPS.filter((step) => step.route === "studio");

    expect(studioSteps.length).toBe(3);
    for (const step of studioSteps) {
      // Clicking inside the spotlight is how the user types the goal or starts
      // the build; auto-advancing there unmounts Studio mid-action.
      expect(
        clickAdvancesTour(step, clickedInside(`[data-onboarding-target="${step.target}"]`)),
      ).toBe(false);
    }
  });

  it("still advances a read-only step from its own spotlight, and only that", () => {
    const last = TOUR_STEPS[TOUR_STEPS.length - 1];

    expect(
      clickAdvancesTour(last, clickedInside(`[data-onboarding-target="${last.target}"]`)),
    ).toBe(true);
    // Clicking the sidebar link for the step's route must not skip the step.
    expect(
      clickAdvancesTour(last, clickedInside(`[data-onboarding-route="${last.route}"]`)),
    ).toBe(false);
  });

  it("gives End tour the same weight as Next", () => {
    vi.stubGlobal("window", { innerWidth: 1280, innerHeight: 800 });
    try {
      const html = renderToStaticMarkup(
        <GuidedTour
          activeTour={TOUR_STEPS[0]}
          busy={false}
          error={null}
          index={0}
          total={TOUR_STEPS.length}
          onBack={() => undefined}
          onComplete={() => undefined}
          onNavigate={() => undefined}
          onNext={() => undefined}
        />,
      );

      // Same control size as Back/Next, not the xs ghost affordance it used to
      // be, and it sits in the action row rather than the popover corner.
      const endTour = buttonClassFor(html, "End tour");
      expect(endTour).toContain("h-8 px-2.5 text-xs");
      expect(endTour).toBe(buttonClassFor(html, "Back"));
      expect(html.indexOf("End tour")).toBeGreaterThan(html.indexOf("Back"));
      expect(html.indexOf("End tour")).toBeLessThan(html.indexOf(">Next<"));
    } finally {
      vi.unstubAllGlobals();
    }
  });
});

describe("tour spotlight targeting", () => {
  function element(name: string): HTMLElement {
    return {
      name,
      getBoundingClientRect: () => ({ width: 200, height: 40 }),
    } as unknown as HTMLElement;
  }

  function stubDom(targets: Record<string, HTMLElement>, routes: Record<string, HTMLElement>) {
    vi.stubGlobal("document", {
      querySelectorAll: (selector: string) => {
        const target = /data-onboarding-target="([^"]+)"/.exec(selector)?.[1];
        if (target) return targets[target] ? [targets[target]] : [];
        const route = /data-onboarding-route="([^"]+)"/.exec(selector)?.[1];
        return route && routes[route] ? [routes[route]] : [];
      },
    });
    vi.stubGlobal("window", {
      getComputedStyle: () => ({ visibility: "visible", display: "block" }),
    });
  }

  it("anchors to the step's own target when the page has mounted", () => {
    const goalField = element("goal-field");
    stubDom({ "studio-brief": goalField }, { studio: element("sidebar-link") });
    try {
      expect(findVisibleTourTarget(TOUR_STEPS[0])).toBe(goalField);
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("stays unanchored instead of spotlighting the sidebar link", () => {
    // The tour navigates, then measures — the target is always absent for at
    // least one frame. Falling back to the route's nav item put a confident
    // ring on the wrong control while the popover described another.
    stubDom({}, { studio: element("sidebar-link") });
    try {
      expect(findVisibleTourTarget(TOUR_STEPS[0])).toBeNull();
    } finally {
      vi.unstubAllGlobals();
    }
  });
});

// Every step points at a `data-onboarding-target` on the page it navigates to.
// Two earlier steps (control-policy, control-timeline) outlived the nodes they
// spotlighted and shipped pointing at nothing, so the link is checked here.
//
// This RENDERS each page in its default state rather than grepping its source:
// a target inside a conditional branch is in the source but not on screen, and
// that is exactly the difference the walkthrough copy depends on.
describe("tour targets exist on the page each step navigates to", () => {
  const PAGE_FOR_ROUTE: Record<string, { module: string; markup: () => string }> = {
    studio: { module: "./AgentStudio.tsx", markup: () => renderPage("studio") },
    "my-agents": { module: "./MyAgents.tsx", markup: () => renderPage("my-agents") },
    activity: { module: "./Activity.tsx", markup: () => renderPage("activity") },
  };

  /** A tour page mounted exactly as the keep-alive route host mounts it. */
  function renderPage(route: "studio" | "my-agents" | "activity"): string {
    const path = route === "activity" ? "/activity" : `/${route}`;
    const page =
      route === "studio" ? (
        <AgentStudio />
      ) : route === "my-agents" ? (
        <MyAgents />
      ) : (
        <Activity />
      );
    return renderToStaticMarkup(
      <StaticRouter location={path}>
        <DashboardSectionCacheProvider>
          <Routes>
            <Route path={path} element={page}>
              <Route path=":id" element={null} />
              <Route path=":id/:section" element={null} />
            </Route>
          </Routes>
        </DashboardSectionCacheProvider>
      </StaticRouter>,
    );
  }

  it("covers every route the walkthrough visits", () => {
    for (const step of TOUR_STEPS) {
      expect(
        PAGE_FOR_ROUTE[step.route],
        `TOUR_STEPS visits "${step.route}" but this test has no page mapped for it`,
      ).toBeDefined();
    }
  });

  for (const step of TOUR_STEPS) {
    const conditional = Boolean(step.revealedBy);
    const claim = conditional
      ? `"${step.label}" declares that ${step.route} renders its control conditionally`
      : `"${step.label}" anchors to a node ${step.route} renders on first paint`;

    it(claim, () => {
      const page = PAGE_FOR_ROUTE[step.route];
      const attribute = `data-onboarding-target="${step.target}"`;
      const onScreen = page.markup().includes(attribute);

      if (conditional) {
        // The step supplies its own note precisely because the control is not
        // there yet; if the page starts rendering it unconditionally, drop
        // `revealedBy` rather than telling the user to go reveal it.
        expect(
          onScreen,
          `${page.module} renders ${attribute} on first paint, so the step's ` +
            "revealedBy note now tells the user to reveal a control that is " +
            "already on screen.",
        ).toBe(false);
        // ...but the target must still exist somewhere, or it is a dead anchor
        // no user action can ever reveal.
        const source = readFileSync(
          fileURLToPath(new URL(page.module, import.meta.url)),
          "utf8",
        );
        expect(
          source.includes(attribute),
          `${page.module} has no ${attribute} at all — no user action can ` +
            "reveal it, so the step can only ever show its note.",
        ).toBe(true);
        return;
      }

      expect(
        onScreen,
        `${page.module} does not render ${attribute} in its default state — ` +
          "the step would show a popover with no spotlight. Either anchor it " +
          "to a control the page always renders, or give the step a " +
          "`revealedBy` note saying what brings the control on screen.",
      ).toBe(true);
    });
  }
});

describe("Studio spotlight anchors", () => {
  const source = readFileSync(
    fileURLToPath(new URL("./AgentStudio.tsx", import.meta.url)),
    "utf8",
  );

  /** Roughly one JSX element's worth of text starting at `attribute`. */
  function attributesAfter(attribute: string): string {
    const at = source.indexOf(attribute);
    expect(at, `${attribute} is missing from AgentStudio.tsx`).toBeGreaterThan(-1);
    return source.slice(at, at + 260);
  }

  it("points the build step at the control that actually starts a run", () => {
    expect(attributesAfter('data-onboarding-target="studio-build"')).toContain(
      'execute("build_new")',
    );
  });

  it("keeps the reuse check from masquerading as the build", () => {
    const reuse = attributesAfter('data-onboarding-target="studio-start"');

    expect(reuse).toContain("resolve()");
    expect(reuse).not.toContain("execute(");
  });

  it("only renders the build control after the reuse check has run", () => {
    // "Build new anyway" lives inside `{resolution && ...}`, so unlike every
    // other tour target it does NOT exist on a cold Studio load. That is why
    // its step carries a `revealedBy` note instead of a bare spotlight, and why
    // GuidedTour keeps watching after the ceiling: the ring lands on the button
    // the moment the reuse check produces it.
    const target = source.indexOf('data-onboarding-target="studio-build"');
    const guard = source.lastIndexOf("{resolution && (", target);

    expect(guard).toBeGreaterThan(-1);
    // The guarded panel is still open where the target sits.
    expect(source.slice(guard, target)).not.toContain("</SurfacePanel>");

    const step = TOUR_STEPS.find((entry) => entry.target === "studio-build");
    expect(
      step?.revealedBy,
      "studio-build is conditionally rendered, so its step must say what " +
        "reveals it rather than claiming the control is on screen.",
    ).toBeTruthy();
  });
});

describe("zero-agent empty state", () => {
  it("ships the actions it tells the user to take", () => {
    const html = renderToStaticMarkup(
      <StaticRouter location="/my-agents">
        <NoAgentsYetState size="comfortable" />
      </StaticRouter>,
    );

    expect(html).toContain("No agents yet");
    expect(html).toContain("Describe an agent");
    expect(html).toContain('href="/studio"');
    expect(html).toContain("Import an existing endpoint");
    expect(html).toContain('href="/my-agents/import"');
  });
});
