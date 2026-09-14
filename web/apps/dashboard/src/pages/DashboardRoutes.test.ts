import { describe, expect, it } from "vitest";
import { Suspense, createElement, isValidElement, type ReactElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { Navigate, matchRoutes, type RouteObject } from "react-router-dom";
import {
  DASHBOARD_LAYOUT_ROUTE_IDS,
  RoutePageErrorBoundary,
  RoutePageErrorFallback,
  createDashboardRouteObjects,
  dashboardRouteChildPaths,
  dashboardLayoutIdForRoute,
  dashboardRoutePath,
  isStaleChunkError,
} from "./DashboardRoutes";
import {
  DASHBOARD_ROUTES,
  DEFAULT_ROUTE_ID,
  ROUTE_BY_ID,
  type DashboardRouteId,
} from "../navigation";
import { ScrollRouteFrame } from "../components/DashboardChrome";
import { DASHBOARD_SECTION_CACHE_KEYS } from "../components/DashboardSectionCache";
import { TOUR_STEPS } from "../components/onboarding/tourSteps";

const ENABLED_FLAGS = new Set([
  "dashboard.simulations",
  "dashboard.bounties",
]);

function routeIdsFor(pathname: string) {
  return (matchRoutes(createDashboardRouteObjects(ENABLED_FLAGS), pathname) ?? [])
    .map((match) => match.route.id)
    .filter(Boolean);
}

function routeIdsForWithFlags(pathname: string, flags: Set<string>) {
  return (matchRoutes(createDashboardRouteObjects(flags), pathname) ?? [])
    .map((match) => match.route.id)
    .filter(Boolean);
}

function expectRoute(pathname: string, layoutId: string, routeId: DashboardRouteId) {
  expect(routeIdsFor(pathname)).toEqual(
    expect.arrayContaining([
      `dashboard-layout-${layoutId}`,
      `dashboard-route-${routeId}`,
    ]),
  );
}

function flattenRouteObjects(routes: RouteObject[]): RouteObject[] {
  return routes.flatMap((route) => [
    route,
    ...flattenRouteObjects(route.children ?? []),
  ]);
}

function rootRouteObject() {
  return createDashboardRouteObjects(ENABLED_FLAGS)[0];
}

function sectionLayoutRouteObjects() {
  return flattenRouteObjects(createDashboardRouteObjects(ENABLED_FLAGS)).filter((route) => {
    const routeId = String(route.id ?? "");
    return routeId.startsWith("dashboard-layout-") && routeId !== "dashboard-layout-root";
  });
}

function dashboardRouteIds(pathname: string) {
  return routeIdsFor(pathname).filter((routeId) =>
    String(routeId).startsWith("dashboard-"),
  );
}

function routeParamsFor(pathname: string) {
  return matchRoutes(createDashboardRouteObjects(ENABLED_FLAGS), pathname)?.at(-1)
    ?.params ?? {};
}

function sectionAndPageRouteIds(pathname: string) {
  return dashboardRouteIds(pathname).filter((routeId) => routeId !== "dashboard-layout-root");
}

const REACT_LAZY = Symbol.for("react.lazy");

function pageRouteObjects() {
  return flattenRouteObjects(createDashboardRouteObjects(ENABLED_FLAGS)).filter((route) =>
    String(route.id ?? "").startsWith("dashboard-route-"),
  );
}

function pageRouteId(route: RouteObject) {
  return String(route.id).replace("dashboard-route-", "") as DashboardRouteId;
}

function elementProps(node: ReactElement | null): Record<string, unknown> {
  return (node?.props ?? {}) as Record<string, unknown>;
}

/**
 * Peel the shell `routePageElement` builds around a page:
 * ScrollRouteFrame? -> RoutePageErrorBoundary -> Suspense -> page element.
 */
function pageElementLayers(element: unknown) {
  let node: unknown = element;
  let scrollFrame = false;
  if (isValidElement(node) && node.type === ScrollRouteFrame) {
    scrollFrame = true;
    node = elementProps(node).children;
  }
  const boundary =
    isValidElement(node) && node.type === RoutePageErrorBoundary ? node : null;
  const inner: unknown = elementProps(boundary).children;
  const suspense = isValidElement(inner) && inner.type === Suspense ? inner : null;
  const page: unknown = elementProps(suspense).children;
  return { scrollFrame, boundary, suspense, page };
}

function isLazyElement(node: unknown) {
  if (!isValidElement(node)) return false;
  const type = node.type as unknown as { $$typeof?: symbol } | null;
  return typeof type === "object" && type !== null && type.$$typeof === REACT_LAZY;
}

function concreteRoutePattern(pattern: string) {
  return pattern
    .replace(/:([A-Za-z0-9_]+)/g, (_match, name: string) => {
      if (name.toLowerCase().includes("section")) return "metadata";
      if (name.toLowerCase().includes("view")) return "overview";
      if (name.toLowerCase().includes("kind")) return "dag";
      return `sample-${name.replace(/[A-Z]/g, (letter) => `-${letter.toLowerCase()}`)}`;
    })
    .replace(/\*$/, "nested/path");
}

describe("DashboardRoutes persistent layouts", () => {
  it("wraps every dashboard route in one persistent root layout", () => {
    const rootRoute = rootRouteObject();

    expect(rootRoute.id).toBe("dashboard-layout-root");
    expect(rootRoute.path).toBe("/");
    expect(rootRoute.children?.length).toBeGreaterThan(
      Object.keys(DASHBOARD_LAYOUT_ROUTE_IDS).length,
    );

    for (const route of DASHBOARD_ROUTES) {
      expect(dashboardRouteIds(route.path)[0]).toBe("dashboard-layout-root");
    }
  });

  it("assigns every dashboard route to one persistent section layout", () => {
    const assigned = Object.values(DASHBOARD_LAYOUT_ROUTE_IDS).flat();
    const routeIds = DASHBOARD_ROUTES.map((route) => route.id);

    expect(new Set(assigned).size).toBe(assigned.length);
    expect([...assigned].sort()).toEqual([...routeIds].sort());
  });

  it("has section cache namespaces for every persistent layout", () => {
    expect(Object.keys(DASHBOARD_SECTION_CACHE_KEYS).sort()).toEqual(
      Object.keys(DASHBOARD_LAYOUT_ROUTE_IDS).sort(),
    );

    for (const layoutId of Object.keys(DASHBOARD_LAYOUT_ROUTE_IDS)) {
      const keys = Object.values(
        DASHBOARD_SECTION_CACHE_KEYS[layoutId as keyof typeof DASHBOARD_SECTION_CACHE_KEYS],
      );
      expect(keys.length).toBeGreaterThan(0);
      expect(keys.every((key) => key.startsWith(`${layoutId}.`))).toBe(true);
    }
  });

  it("uses pathless persistent parents for every section layout", () => {
    const layoutRoutes = sectionLayoutRouteObjects();

    expect(layoutRoutes).toHaveLength(Object.keys(DASHBOARD_LAYOUT_ROUTE_IDS).length);

    for (const layoutRoute of layoutRoutes) {
      const layoutId = String(layoutRoute.id).replace(
        "dashboard-layout-",
        "",
      ) as keyof typeof DASHBOARD_LAYOUT_ROUTE_IDS;
      const childRouteIds = (layoutRoute.children ?? [])
        .map((child) => String(child.id).replace("dashboard-route-", ""))
        .sort();

      expect(layoutRoute.path).toBeUndefined();
      expect(childRouteIds).toEqual(
        [...DASHBOARD_LAYOUT_ROUTE_IDS[layoutId]].sort(),
      );
    }
  });

  it("keeps sibling page transitions on the same persistent section parent", () => {
    for (const [layoutId, routeIds] of Object.entries(DASHBOARD_LAYOUT_ROUTE_IDS)) {
      if (routeIds.length < 2) continue;

      for (let index = 1; index < routeIds.length; index += 1) {
        const fromRoute = ROUTE_BY_ID[routeIds[index - 1]];
        const toRoute = ROUTE_BY_ID[routeIds[index]];
        const fromIds = sectionAndPageRouteIds(fromRoute.path);
        const toIds = sectionAndPageRouteIds(toRoute.path);

        expect(fromIds[0]).toBe(`dashboard-layout-${layoutId}`);
        expect(toIds[0]).toBe(`dashboard-layout-${layoutId}`);
        expect(fromIds[1]).toBe(`dashboard-route-${fromRoute.id}`);
        expect(toIds[1]).toBe(`dashboard-route-${toRoute.id}`);
        expect(fromIds[1]).not.toBe(toIds[1]);
      }
    }
  });

  it("keeps workspace subviews inside one mounted workspace page route", () => {
    const workspaceRouteIds = [
      "/workspace",
      "/workspace/threads",
      "/workspace/settings",
      "/workspace/files",
      "/workspace/artifacts",
      "/workspace/activity",
    ].map(dashboardRouteIds);

    for (const ids of workspaceRouteIds) {
      expect(ids).toEqual([
        "dashboard-layout-root",
        "dashboard-layout-workspace",
        "dashboard-route-workspace",
      ]);
    }
  });

  it("uses one owning route plus child patterns per dashboard page", () => {
    for (const route of DASHBOARD_ROUTES) {
      expect(dashboardRoutePath(route)).toBe(route.path);
      expect(dashboardRouteChildPaths(route).at(-1)).toBe("*");
    }
  });

  it("keeps every declared deep route pattern under its owning layout", () => {
    for (const route of DASHBOARD_ROUTES) {
      for (const pattern of route.routePatterns ?? []) {
        expectRoute(
          concreteRoutePattern(pattern),
          dashboardLayoutIdForRoute(route.id),
          route.id,
        );
      }
    }
  });

  it("exposes named params from deep dashboard routes to their mounted page", () => {
    expect(routeParamsFor("/my-agents/agent-1/proofs/proof-1")).toMatchObject({
      agentName: "agent-1",
      proofId: "proof-1",
    });
    expect(routeParamsFor("/my-agents/agent-1/runs/grant-1/files")).toMatchObject({
      agentName: "agent-1",
      grantId: "grant-1",
      runView: "files",
    });
    expect(routeParamsFor("/workspace/activity/dag/job-1/timeline")).toMatchObject({
      detailKind: "dag",
      detailId: "job-1",
      detailSection: "timeline",
    });
  });

  it("keeps feature-gated deep links routeable when their flags are disabled", () => {
    expect(routeIdsForWithFlags("/simulations/runs/job-1/trace", new Set()))
      .toContain("dashboard-route-simulations");
    expect(routeIdsForWithFlags("/bounties/open/request-1/receipt", new Set()))
      .toContain("dashboard-route-bounties");
  });

  it("keeps workspace URLs under the same layout and page route", () => {
    expectRoute("/workspace", "workspace", "workspace");
    expectRoute("/workspace/files", "workspace", "workspace");
    expectRoute("/workspace/files/file/src/App.tsx", "workspace", "workspace");
    expectRoute("/workspace/activity/dag/job-1/timeline", "workspace", "workspace");
  });

  it("keeps operate sibling pages under one layout", () => {
    expectRoute("/activity/job-1/events", "operate", "activity");
    expectRoute("/trials/room-1/runs/run-1/receipts", "operate", "trials");
    expectRoute("/schedules/list/schedule-1/metadata", "operate", "schedules");
  });

  it("keeps agent sibling pages under one layout", () => {
    expectRoute("/my-agents/agent-1/proofs/proof-1", "agents", "my-agents");
    expectRoute("/marketplace/agents/agent-1/install", "agents", "marketplace");
    expectRoute("/compose/runs/composed/run-1/events", "agents", "compose");
    expectRoute("/installed-setup/agent-1/auth", "agents", "installed-agents");
    expectRoute("/bounties/open/request-1/receipt", "agents", "bounties");
  });

  it("keeps runtime pages under one layout", () => {
    expectRoute("/runtime/timeline/receipts/receipt-1/payload", "runtime", "runtime");
    expectRoute("/simulations/runs/job-1/trace", "runtime", "simulations");
  });

  it("keeps settings pages under one layout", () => {
    expectRoute("/access/repositories/agent-1", "settings", "access");
    expectRoute("/llm-keys/saved/default/usage", "settings", "keys");
    expectRoute("/organization/acme/members/member-1", "settings", "organization");
  });

  it("exposes the layout assignment helper from the same grouping", () => {
    expect(dashboardLayoutIdForRoute("workspace")).toBe("workspace");
    expect(dashboardLayoutIdForRoute("activity")).toBe("operate");
    expect(dashboardLayoutIdForRoute("marketplace")).toBe("agents");
    expect(dashboardLayoutIdForRoute("runtime")).toBe("runtime");
    expect(dashboardLayoutIdForRoute("keys")).toBe("settings");
  });

  it("preserves route redirects for aliases", () => {
    expect(routeIdsFor("/keys")).toEqual([
      "dashboard-layout-root",
      "dashboard-redirect-keys-/keys",
    ]);
    expect(routeIdsFor("/control-room")).toEqual([
      "dashboard-layout-root",
      "dashboard-redirect-runtime-/control-room",
    ]);
    expect(ROUTE_BY_ID.keys.redirects?.[0]?.path).toBe("/keys");
    expect(ROUTE_BY_ID.runtime.redirects?.some((redirect) => redirect.path === "/control-room"))
      .toBe(true);
  });

  it("keeps the index redirect and the catch-all pointing at the default route", () => {
    const defaultPath = ROUTE_BY_ID[DEFAULT_ROUTE_ID].path;
    const rootChildren = rootRouteObject().children ?? [];
    const indexRoute = rootChildren.find((child) => child.index);
    const catchAllRoute = rootChildren.find((child) => child.path === "*");

    for (const [label, route] of [
      ["index", indexRoute],
      ["catch-all", catchAllRoute],
    ] as const) {
      expect(route, `${label} route is missing`).toBeDefined();
      const element = route?.element as ReactElement;
      expect(element.type).toBe(Navigate);
      expect(element.props.to).toBe(defaultPath);
      expect(element.props.replace).toBe(true);
    }

    expect(
      matchRoutes(createDashboardRouteObjects(ENABLED_FLAGS), "/no-such-page")?.at(-1)
        ?.route.path,
    ).toBe("*");
  });
});

// The bundle shape below is load-bearing, and `matchRoutes` cannot see it: these
// tests inspect the route ELEMENTS, so a refactor that lazy-loads the landing
// route, lazy-loads a page the onboarding tour spotlights, or drops the Suspense
// or error boundary fails here instead of in production.
describe("DashboardRoutes code splitting", () => {
  const tourRouteIds = new Set<string>(TOUR_STEPS.map((step) => step.route));
  const staticRouteIds = new Set<string>([DEFAULT_ROUTE_ID, ...tourRouteIds]);

  it("keeps the default landing page in the entry bundle", () => {
    const landing = pageRouteObjects().find(
      (route) => pageRouteId(route) === DEFAULT_ROUTE_ID,
    );
    const { page } = pageElementLayers(landing?.element);

    expect(isValidElement(page)).toBe(true);
    expect(
      isLazyElement(page),
      `${DEFAULT_ROUTE_ID} is the first screen every session paints; code-splitting it adds a round trip before first paint`,
    ).toBe(false);
  });

  it("keeps every onboarding tour page out of the code-split set", () => {
    expect(tourRouteIds.size).toBeGreaterThan(0);

    for (const route of pageRouteObjects()) {
      const routeId = pageRouteId(route);
      if (!tourRouteIds.has(routeId)) continue;
      const { page } = pageElementLayers(route.element);

      expect(
        isLazyElement(page),
        `TOUR_STEPS spotlights "${routeId}", so its page must stay a static import. ` +
          "GuidedTour waits for the target node rather than measuring once, so a late " +
          "chunk no longer mis-anchors the ring — but it does leave the first screen a " +
          "new account sees without a spotlight until the chunk arrives.",
      ).toBe(false);
    }
  });

  it("code-splits every other page", () => {
    const lazyRouteIds = pageRouteObjects()
      .filter((route) => isLazyElement(pageElementLayers(route.element).page))
      .map(pageRouteId);
    const expected = DASHBOARD_ROUTES.map((route) => route.id).filter(
      (routeId) => !staticRouteIds.has(routeId),
    );

    expect([...lazyRouteIds].sort()).toEqual([...expected].sort());
  });

  it("wraps every page in an error boundary above its Suspense fallback", () => {
    for (const route of pageRouteObjects()) {
      const routeId = pageRouteId(route);
      const { boundary, suspense } = pageElementLayers(route.element);

      expect(
        boundary,
        `route "${routeId}" has no error boundary: an uncaught render error unmounts the whole React root`,
      ).not.toBeNull();
      expect(elementProps(boundary).routeId).toBe(routeId);
      expect(
        suspense,
        `route "${routeId}" has no Suspense boundary: a suspending page throws to the nearest boundary instead of showing a fallback`,
      ).not.toBeNull();
      expect(isValidElement(elementProps(suspense).fallback)).toBe(true);
    }
  });

  it("gives the loading fallback the fill rule its parent actually honours", () => {
    for (const route of pageRouteObjects()) {
      const routeId = pageRouteId(route);
      const { scrollFrame, suspense } = pageElementLayers(route.element);
      const markup = renderToStaticMarkup(elementProps(suspense).fallback as ReactElement);

      expect(scrollFrame).toBe(ROUTE_BY_ID[routeId].layout !== "full-height");
      expect(markup).toContain('aria-busy="true"');
      if (scrollFrame) {
        // ScrollRouteFrame is a block container, so `flex-1` on its child is inert.
        expect(markup, `route "${routeId}" fallback must fill the scroll frame`).toContain(
          "min-h-full",
        );
        expect(markup).not.toContain("flex-1");
      } else {
        // Full-height pages hang the fallback straight off the flex-column pane.
        expect(markup, `route "${routeId}" fallback must fill the route pane`).toContain(
          "flex-1",
        );
        expect(markup).not.toContain("min-h-full");
      }
    }
  });

  it("recognises a stale chunk download and nothing else", () => {
    expect(
      isStaleChunkError(
        new Error(
          "Failed to fetch dynamically imported module: https://app.example.com/assets/Compliance-CH9L9gcA.js",
        ),
      ),
    ).toBe(true);
    expect(isStaleChunkError(new Error("error loading dynamically imported module"))).toBe(
      true,
    );
    expect(isStaleChunkError(new Error("Importing a module script failed."))).toBe(true);
    expect(isStaleChunkError(new Error("Unable to preload CSS for /assets/index.css"))).toBe(
      true,
    );
    expect(isStaleChunkError(new TypeError("x is not a function"))).toBe(false);
    expect(isStaleChunkError(null)).toBe(false);
  });

  it("replaces a failed page with a scoped reload prompt instead of rethrowing", () => {
    const error = new Error(
      "Failed to fetch dynamically imported module: /assets/Compliance-CH9L9gcA.js",
    );

    expect(RoutePageErrorBoundary.getDerivedStateFromError(error)).toEqual({ error });
    // A page that throws a falsy value must still latch, or the boundary would
    // re-render the children and throw again on every pass.
    expect(RoutePageErrorBoundary.getDerivedStateFromError(null).error).toBeTruthy();

    const boundary = new RoutePageErrorBoundary({ routeId: "compliance", children: null });
    boundary.state = { error };
    const markup = renderToStaticMarkup(boundary.render() as ReactElement);

    expect(markup).toContain('role="alert"');
    expect(markup).toContain('data-route-error="compliance"');
    expect(markup).toContain("updated after this tab was opened");
    expect(markup).toContain("Reload the dashboard");
  });

  it("keeps a page bug distinguishable from a stale deploy in the same fallback", () => {
    const markup = renderToStaticMarkup(
      createElement(RoutePageErrorFallback, { routeId: "runtime", stale: false }),
    );

    expect(markup).toContain('data-route-error="runtime"');
    expect(markup).toContain("failed to render");
    expect(markup).toContain("Reload the dashboard");
  });
});
