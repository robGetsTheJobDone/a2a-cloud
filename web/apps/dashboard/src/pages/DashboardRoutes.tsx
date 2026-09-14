import {
  Navigate,
  Outlet,
  useLocation,
  useNavigate,
  useRoutes,
  type RouteObject,
} from "react-router-dom";
import {
  Component,
  Suspense,
  lazy,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ErrorInfo,
  type ReactNode,
} from "react";
import {
  InlineAlert,
  ScrollRouteFrame,
  ToolbarButton,
} from "../components/DashboardChrome";
import { DashboardSectionCacheProvider } from "../components/DashboardSectionCache";
import { useDashboardRenderDiagnostic } from "../components/dashboardRenderDiagnostics";
import {
  DASHBOARD_ROUTES,
  DEFAULT_ROUTE_ID,
  ROUTE_BY_ID,
  isRouteEnabled,
  matchDashboardRoute,
  normalizePathname,
  type DashboardRouteConfig,
  type DashboardRouteId,
} from "../navigation";
// Pass-through *Page wrappers deleted (REDESIGN deletion log). The feature components
// own their full routing/state (useParams + section cache), so route to them directly.
//
// Two sets of pages stay STATIC imports on purpose:
//
//  1. WorkspacePage is DEFAULT_ROUTE_ID's page and therefore the first screen
//     every session paints. Code-splitting the landing route would only add a
//     round trip before first paint.
//  2. AgentStudio / MyAgents / Activity are the pages the onboarding walkthrough
//     spotlights (TOUR_STEPS in components/onboarding/tourSteps.ts). GuidedTour
//     watches for the target node (frame poll until TOUR_ANCHOR_CEILING_MS, then
//     a slower recheck for the rest of the step) rather than measuring once, so
//     a late chunk no longer mis-anchors the spotlight — but it does leave the
//     very first screen of a new account with an un-spotlighted popover for as
//     long as the chunk takes to arrive. These are the pages a brand-new account
//     sees first, so keep them in the entry bundle and present in the same commit
//     as the navigation. The "keeps every onboarding tour page out of the
//     code-split set" test fails if a new tour step is ever pointed at a
//     code-split page.
import { WorkspacePage } from "./work/WorkspacePage";
import { Activity } from "../components/Activity";
import { AgentStudio } from "../components/AgentStudio";
import { MyAgents } from "../components/MyAgents";
// Every other page is code-split so a new session no longer downloads and parses
// admin/compliance/marketplace/runtime code it may never open. These
// modules use named exports, so each import() is mapped to the `default` shape
// React.lazy expects. Module identity is stable (one lazy() per module created at
// load), so keep-alive panes reuse the resolved component across navigations.
const RuntimePage = lazy(() =>
  import("./admin/RuntimePage").then((m) => ({ default: m.RuntimePage })),
);
const SimulationsPage = lazy(() =>
  import("./work/SimulationsPage").then((m) => ({ default: m.SimulationsPage })),
);
const TrialsPage = lazy(() =>
  import("./work/TrialsPage").then((m) => ({ default: m.TrialsPage })),
);
const Bounties = lazy(() =>
  import("../components/Bounties").then((m) => ({ default: m.Bounties })),
);
const Compliance = lazy(() =>
  import("../components/Compliance").then((m) => ({ default: m.Compliance })),
);
const InstalledAgents = lazy(() =>
  import("../components/InstalledAgents").then((m) => ({ default: m.InstalledAgents })),
);
const LlmKeys = lazy(() =>
  import("../components/LlmKeys").then((m) => ({ default: m.LlmKeys })),
);
const Marketplace = lazy(() =>
  import("../components/Marketplace").then((m) => ({ default: m.Marketplace })),
);
const MetaAgentCompose = lazy(() =>
  import("../components/MetaAgentCompose").then((m) => ({ default: m.MetaAgentCompose })),
);
const OrganizationAuth = lazy(() =>
  import("../components/OrganizationAuth").then((m) => ({ default: m.OrganizationAuth })),
);
const Schedules = lazy(() =>
  import("../components/Schedules").then((m) => ({ default: m.Schedules })),
);
const ServiceAccess = lazy(() =>
  import("../components/ServiceAccess").then((m) => ({ default: m.ServiceAccess })),
);

type DashboardRoutesProps = {
  enabledFeatureFlags: Set<string>;
};

export type DashboardLayoutId =
  | "workspace"
  | "operate"
  | "agents"
  | "runtime"
  | "settings";

export const DASHBOARD_LAYOUT_ROUTE_IDS: Record<
  DashboardLayoutId,
  readonly DashboardRouteId[]
> = {
  workspace: ["workspace"],
  operate: ["activity", "trials", "schedules"],
  agents: ["my-agents", "marketplace", "studio", "compose", "installed-agents", "bounties"],
  runtime: ["runtime", "simulations"],
  settings: ["access", "keys", "organization", "compliance"],
};

const ROUTE_ELEMENTS: Record<DashboardRouteId, JSX.Element> = {
  workspace: <WorkspacePage />,
  trials: <TrialsPage />,
  activity: <Activity />,
  schedules: <Schedules />,
  simulations: <SimulationsPage />,
  "my-agents": <MyAgents />,
  marketplace: <Marketplace />,
  studio: <AgentStudio />,
  compose: <MetaAgentCompose />,
  "installed-agents": <InstalledAgents />,
  bounties: <Bounties />,
  access: <ServiceAccess />,
  keys: <LlmKeys />,
  organization: <OrganizationAuth />,
  compliance: <Compliance />,
  runtime: <RuntimePage />,
};

function PersistentDashboardLayout({ layoutId }: { layoutId: DashboardLayoutId }) {
  useDashboardRenderDiagnostic(`dashboard-layout:${layoutId}`);

  return (
    <DashboardSectionCacheProvider>
      <div className="contents" data-dashboard-layout={layoutId}>
        <Outlet />
      </div>
    </DashboardSectionCacheProvider>
  );
}

function DashboardLayout() {
  useDashboardRenderDiagnostic("dashboard-layout:root");

  return (
    <div className="contents" data-dashboard-layout="root">
      <Outlet />
    </div>
  );
}

function WorkspaceLayout() {
  return <PersistentDashboardLayout layoutId="workspace" />;
}

function OperateLayout() {
  return <PersistentDashboardLayout layoutId="operate" />;
}

function AgentsLayout() {
  return <PersistentDashboardLayout layoutId="agents" />;
}

function RuntimeLayout() {
  return <PersistentDashboardLayout layoutId="runtime" />;
}

function SettingsLayout() {
  return <PersistentDashboardLayout layoutId="settings" />;
}

export function dashboardRoutePath(route: DashboardRouteConfig) {
  return route.path.replace(/\/+$/, "") || "/";
}

function routePatternChildPath(route: DashboardRouteConfig, pattern: string) {
  const basePath = dashboardRoutePath(route);
  const normalizedPattern = pattern.replace(/\/+$/, "") || "/";
  if (normalizedPattern === basePath) return null;
  const prefix = `${basePath}/`;
  if (!normalizedPattern.startsWith(prefix)) return null;
  const childPath = normalizedPattern.slice(prefix.length);
  return childPath || null;
}

export function dashboardRouteChildPaths(route: DashboardRouteConfig) {
  const childPaths = new Set<string>();
  for (const pattern of route.routePatterns ?? []) {
    const childPath = routePatternChildPath(route, pattern);
    if (childPath && childPath !== "*") childPaths.add(childPath);
  }
  childPaths.add("*");
  return [...childPaths];
}

export function dashboardLayoutIdForRoute(
  routeId: DashboardRouteId,
): DashboardLayoutId {
  const entry = Object.entries(DASHBOARD_LAYOUT_ROUTE_IDS).find(([, routeIds]) =>
    routeIds.includes(routeId),
  );
  if (!entry) {
    throw new Error(`Dashboard route ${routeId} is missing a persistent layout`);
  }
  return entry[0] as DashboardLayoutId;
}

function layoutElement(layoutId: DashboardLayoutId) {
  if (layoutId === "workspace") return <WorkspaceLayout />;
  if (layoutId === "operate") return <OperateLayout />;
  if (layoutId === "agents") return <AgentsLayout />;
  if (layoutId === "runtime") return <RuntimeLayout />;
  return <SettingsLayout />;
}

// Shown only while a code-split page chunk is still in flight. It matches the
// KernelTraceCard skeleton language (runtime tokens + animate-pulse).
//
// `fullHeight` picks the fill rule because the two parents differ: a full-height
// page's fallback is a direct child of the flex-column route pane, so `flex-1`
// fills it, while a scrolling page's fallback sits inside ScrollRouteFrame, which
// is a plain BLOCK container ("min-h-0 flex-1 overflow-auto bg-runtime-bg") where
// `flex-1` would be inert — it fills with `min-h-full` instead. Either way the
// pane keeps its box; the content inside it does still change on resolve, because
// the page header (RoutePageShell) belongs to the page module, not to this shell.
function RoutePageFallback({ fullHeight }: { fullHeight: boolean }) {
  return (
    <div
      role="status"
      aria-label="Loading page"
      aria-busy="true"
      className={`animate-pulse space-y-3 bg-runtime-bg p-4 ${
        fullHeight ? "min-h-0 flex-1" : "min-h-full"
      }`}
    >
      <div className="h-5 w-48 rounded bg-runtime-line/70" />
      <div className="h-3 w-72 rounded bg-runtime-line/50" />
      <div className="grid gap-3 sm:grid-cols-3">
        <div className="h-20 rounded bg-runtime-panel/60" />
        <div className="h-20 rounded bg-runtime-panel/60" />
        <div className="h-20 rounded bg-runtime-panel/60" />
      </div>
    </div>
  );
}

// Browser wording for "the JS chunk this page needs did not load", per engine.
const STALE_CHUNK_ERROR_PATTERNS = [
  "failed to fetch dynamically imported module", // Chromium
  "error loading dynamically imported module", // Firefox
  "importing a module script failed", // Safari
  "unable to preload css", // Vite's preload helper
];

/**
 * True when an error means "this page's chunk could not be downloaded" rather
 * than "this page has a bug".
 *
 * This is a routine production event, not an edge case: deploy/dashboard.yaml
 * runs one replica with `strategy: Recreate`, and nginx.conf serves `/assets/`
 * with `try_files $uri =404` (no SPA fallback), so the moment a new image rolls
 * out every previously built chunk filename starts returning 404. Any tab left
 * open across a deploy hits this on its first visit to a page it had not already
 * opened.
 */
export function isStaleChunkError(error: unknown): boolean {
  const message = (
    error instanceof Error ? error.message : typeof error === "string" ? error : ""
  ).toLowerCase();
  return STALE_CHUNK_ERROR_PATTERNS.some((pattern) => message.includes(pattern));
}

export function RoutePageErrorFallback({
  routeId,
  stale,
}: {
  routeId: DashboardRouteId;
  stale: boolean;
}) {
  return (
    <div
      role="alert"
      data-route-error={routeId}
      className="min-h-full space-y-3 bg-runtime-bg p-4"
    >
      <InlineAlert tone="amber">
        {stale
          ? "This page could not be loaded because the dashboard was updated after this tab was opened. Reload to pick up the new version."
          : "This page failed to render. Reloading usually clears it."}
      </InlineAlert>
      {/* Recovery is an explicit click, never an automatic reload: the other
          route panes are keep-alive and may hold unsaved work (a workspace chat
          draft), and reloading out from under the user would discard it. */}
      <ToolbarButton variant="primary" onClick={() => window.location.reload()}>
        Reload the dashboard
      </ToolbarButton>
    </div>
  );
}

type RoutePageErrorBoundaryProps = {
  routeId: DashboardRouteId;
  children: ReactNode;
};

type RoutePageErrorBoundaryState = { error: unknown };

/**
 * Keeps a failed page inside its own route pane.
 *
 * Code-splitting means a page can now fail during RENDER with a network error
 * (see isStaleChunkError). React 18 unmounts the entire root on an uncaught
 * render error, and src/main.tsx mounts the router with no boundary above it, so
 * without this a single stale click would blank the whole dashboard. With it the
 * blast radius is one pane: the sidebar and every other keep-alive pane stay
 * mounted and usable.
 */
export class RoutePageErrorBoundary extends Component<
  RoutePageErrorBoundaryProps,
  RoutePageErrorBoundaryState
> {
  state: RoutePageErrorBoundaryState = { error: null };

  static getDerivedStateFromError(error: unknown): RoutePageErrorBoundaryState {
    // Always store something truthy: a page that threw a falsy value would
    // otherwise re-render its children and throw again on every pass.
    return { error: error ?? new Error("Page failed to render") };
  }

  componentDidCatch(error: unknown, info: ErrorInfo) {
    console.error(`Route "${this.props.routeId}" failed to render`, error, info);
  }

  render() {
    const { error } = this.state;
    if (!error) return this.props.children;
    return (
      <RoutePageErrorFallback
        routeId={this.props.routeId}
        stale={isStaleChunkError(error)}
      />
    );
  }
}

function routePageElement(
  route: DashboardRouteConfig,
  element: JSX.Element,
  enabledFeatureFlags: Set<string>,
  defaultPath: string,
) {
  if (!isRouteEnabled(route, enabledFeatureFlags)) {
    return <Navigate to={defaultPath} replace />;
  }
  // The boundary and its Suspense sit INSIDE ScrollRouteFrame so the scroll frame
  // (and its background) stays mounted across pending -> resolved -> failed, and
  // inside the route pane so one loading or broken page never blanks the sibling
  // keep-alive panes.
  const page = (
    <RoutePageErrorBoundary routeId={route.id}>
      <Suspense
        fallback={<RoutePageFallback fullHeight={route.layout === "full-height"} />}
      >
        {element}
      </Suspense>
    </RoutePageErrorBoundary>
  );
  return route.layout === "full-height" ? page : (
    <ScrollRouteFrame>
      {page}
    </ScrollRouteFrame>
  );
}

function routeRedirectElement(
  route: DashboardRouteConfig,
  to: string,
  enabledFeatureFlags: Set<string>,
  defaultPath: string,
) {
  return isRouteEnabled(route, enabledFeatureFlags) ? (
    <Navigate to={to} replace />
  ) : (
    <Navigate to={defaultPath} replace />
  );
}

export function createDashboardRouteObjects(
  enabledFeatureFlags: Set<string>,
): RouteObject[] {
  const defaultPath = ROUTE_BY_ID[DEFAULT_ROUTE_ID].path;
  const groupedRoutes = Object.entries(DASHBOARD_LAYOUT_ROUTE_IDS).map(
    ([layoutId, routeIds]) => ({
      id: `dashboard-layout-${layoutId}`,
      element: layoutElement(layoutId as DashboardLayoutId),
      children: routeIds.map((routeId) => {
        const route = ROUTE_BY_ID[routeId];
        return {
          id: `dashboard-route-${route.id}`,
          path: dashboardRoutePath(route),
          element: routePageElement(
            route,
            ROUTE_ELEMENTS[route.id],
            enabledFeatureFlags,
            defaultPath,
          ),
          children: dashboardRouteChildPaths(route).map((path, index) => ({
            id: `route-pattern-${route.id}-${index}`,
            path,
            element: null,
          })),
        };
      }),
    }),
  );

  return [
    {
      id: "dashboard-layout-root",
      path: "/",
      element: <DashboardLayout />,
      children: [
        { index: true, element: <Navigate to={defaultPath} replace /> },
        ...groupedRoutes,
        ...DASHBOARD_ROUTES.flatMap((route) =>
          (route.redirects ?? []).map((redirect) => ({
            id: `dashboard-redirect-${route.id}-${redirect.path}`,
            path: redirect.path,
            element: routeRedirectElement(
              route,
              redirect.to ?? route.path,
              enabledFeatureFlags,
              defaultPath,
            ),
          })),
        ),
        { path: "*", element: <Navigate to={defaultPath} replace /> },
      ],
    },
  ];
}

// --- Keep-alive route host --------------------------------------------------
// Route pages NEVER unmount. Each visited route mounts once into a persistent pane
// and is shown/hidden on navigation instead of being destroyed + recreated. Each
// pane runs its OWN `useRoutes` against either the live location (when active) or a
// frozen snapshot of its last-active location (when hidden) — this reuses the exact
// matching tree from `createDashboardRouteObjects` so `useParams`/`useSearchParams`
// resolve identically to before, while the component instance (and its scroll, local
// state, and in-flight work) survives navigation. Section-cache providers for all
// layout groups stay mounted so data persists across group switches too (mandate B).
// Verify with `dashboardRenderDiagnostics`: a pane's unmountCount must stay 0 after nav.

function routePaneObjects(
  route: DashboardRouteConfig,
  enabledFeatureFlags: Set<string>,
  defaultPath: string,
): RouteObject[] {
  return [
    {
      id: `pane-route-${route.id}`,
      path: dashboardRoutePath(route),
      element: routePageElement(
        route,
        ROUTE_ELEMENTS[route.id],
        enabledFeatureFlags,
        defaultPath,
      ),
      children: dashboardRouteChildPaths(route).map((path, index) => ({
        id: `pane-pattern-${route.id}-${index}`,
        path,
        element: null,
      })),
    },
  ];
}

type RoutePaneProps = {
  route: DashboardRouteConfig;
  active: boolean;
  paneLocation: string | undefined;
  enabledFeatureFlags: Set<string>;
  defaultPath: string;
};

function RoutePane({
  route,
  active,
  paneLocation,
  enabledFeatureFlags,
  defaultPath,
}: RoutePaneProps) {
  useDashboardRenderDiagnostic(`dashboard-pane:${route.id}`);
  const objects = useMemo(
    () => routePaneObjects(route, enabledFeatureFlags, defaultPath),
    [route, enabledFeatureFlags, defaultPath],
  );
  // Active pane tracks the live location (undefined arg); hidden panes render their
  // frozen last-active location so they keep their view and ignore other routes' URLs.
  const element = useRoutes(objects, active ? undefined : paneLocation);

  return (
    <div
      // `display` MUST be conditional: a static `flex` utility overrides the
      // [hidden] attribute's UA `display:none`, which would leave every visited
      // keep-alive pane stacked on screen. Only the active pane is a flex column.
      className={`route-pane min-h-0 min-w-0 flex-1 flex-col ${active ? "flex" : "hidden"}`}
      data-route-pane={route.id}
      data-active={active ? "true" : "false"}
      hidden={!active}
    >
      {element}
    </div>
  );
}

export function DashboardRoutes({ enabledFeatureFlags }: DashboardRoutesProps) {
  useDashboardRenderDiagnostic("dashboard-route-host");
  const location = useLocation();
  const navigate = useNavigate();
  const defaultPath = ROUTE_BY_ID[DEFAULT_ROUTE_ID].path;

  // Canonicalize alias/redirect URLs (e.g. /keys -> /llm-keys, /control-room ->
  // /runtime) in place, without ever unmounting the destination pane.
  useEffect(() => {
    const normalized = normalizePathname(location.pathname);
    for (const route of DASHBOARD_ROUTES) {
      for (const redirect of route.redirects ?? []) {
        if (normalizePathname(redirect.path) === normalized) {
          const target = redirect.to ?? route.path;
          navigate(`${target}${location.search}`, { replace: true });
          return;
        }
      }
    }
  }, [location.pathname, location.search, navigate]);

  const match = matchDashboardRoute(location.pathname);
  const activeRouteId =
    match && isRouteEnabled(match.route, enabledFeatureFlags) ? match.route.id : null;

  // Visited routes mount once then persist for the session.
  const [visited, setVisited] = useState<Set<DashboardRouteId>>(() =>
    activeRouteId ? new Set([activeRouteId]) : new Set(),
  );
  useEffect(() => {
    if (!activeRouteId) return;
    setVisited((current) =>
      current.has(activeRouteId) ? current : new Set(current).add(activeRouteId),
    );
  }, [activeRouteId]);

  // Frozen last-active full path per route (consumed by hidden panes).
  const frozenRef = useRef<Map<DashboardRouteId, string>>(new Map());
  if (activeRouteId) {
    frozenRef.current.set(activeRouteId, `${location.pathname}${location.search}`);
  }

  const layoutEntries = Object.entries(DASHBOARD_LAYOUT_ROUTE_IDS) as Array<
    [DashboardLayoutId, readonly DashboardRouteId[]]
  >;

  return (
    <div
      className="flex min-h-0 min-w-0 flex-1 flex-col [overflow-x:clip]"
      data-dashboard-route-host
    >
      {layoutEntries.map(([layoutId, routeIds]) => (
        <DashboardSectionCacheProvider key={layoutId}>
          {routeIds.map((routeId) => {
            if (!visited.has(routeId)) return null;
            const route = ROUTE_BY_ID[routeId];
            if (!isRouteEnabled(route, enabledFeatureFlags)) return null;
            return (
              <RoutePane
                key={routeId}
                route={route}
                active={routeId === activeRouteId}
                paneLocation={frozenRef.current.get(routeId)}
                enabledFeatureFlags={enabledFeatureFlags}
                defaultPath={defaultPath}
              />
            );
          })}
        </DashboardSectionCacheProvider>
      ))}
    </div>
  );
}
