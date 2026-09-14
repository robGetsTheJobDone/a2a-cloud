import {
  memo,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type Dispatch,
  type MutableRefObject,
  type MouseEvent,
  type SetStateAction,
} from "react";
import {
  Link,
  useLocation,
  useNavigate,
  type NavigateFunction,
} from "react-router-dom";
import {
  browserSession,
  getFeatureFlags,
  getOnboardingState,
  logout as logoutSession,
  session,
  type FeatureFlags,
  type OnboardingState,
  type Session,
} from "./api";
import { clearAnalytics, identifyAnalytics, trackEvent } from "./analytics";
import { Login } from "./components/Login";
import {
  CommandPalette,
  ToolbarButton,
} from "./components/DashboardChrome";
import { OnboardingWizard } from "./components/OnboardingWizard";
import { Icon } from "./components/Icon";
import { useDashboardRenderDiagnostic } from "./components/dashboardRenderDiagnostics";
import {
  DEFAULT_ROUTE_ID,
  ROUTE_BY_ID,
  activeDashboardNavItem,
  commandNavigationItems,
  isRouteEnabled,
  matchDashboardRoute,
  searchCommandNavigationItems,
  visibleDashboardNavSections,
  type DashboardNavItem,
  type DashboardNavSection,
  type DashboardRouteId,
} from "./navigation";
import { DashboardRoutes } from "./pages/DashboardRoutes";

function cx(...classes: Array<string | false | null | undefined>) {
  return classes.filter(Boolean).join(" ");
}

function clearHashFromUrl() {
  const url = new URL(window.location.href);
  url.hash = "";
  window.history.replaceState(null, "", `${url.pathname}${url.search}`);
}

function currentRedirectPath(): string {
  const path = `${window.location.pathname}${window.location.search}`;
  return path === "/" ? ROUTE_BY_ID.workspace.path : path;
}

export default function App() {
  const [user, setUser] = useState<Session | null>(null);
  const [ready, setReady] = useState(false);
  const [authError, setAuthError] = useState<string | null>(null);
  const [featureFlags, setFeatureFlags] = useState<FeatureFlags>({ enabled_keys: [] });
  const [onboardingState, setOnboardingState] = useState<OnboardingState | null>(null);

  useEffect(() => {
    let cancelled = false;
    async function loadSession() {
      const hash = new URLSearchParams(window.location.hash.replace(/^#/, ""));
      const authError = hash.get("auth_error");
      if (authError) {
        if (cancelled) return;
        setAuthError(authError);
        clearHashFromUrl();
      }
      try {
        const remote = await browserSession();
        if (cancelled) return;
        if (remote) session.save(remote);
        setUser(remote);
      } catch {
        if (cancelled) return;
        session.clear();
        setUser(null);
      } finally {
        if (!cancelled) setReady(true);
      }
    }
    void loadSession();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!user) {
      setFeatureFlags({ enabled_keys: [] });
      clearAnalytics();
      return;
    }
    identifyAnalytics(user);
  }, [user]);

  useEffect(() => {
    let cancelled = false;
    if (!user) {
      setFeatureFlags({ enabled_keys: [] });
      return () => {
        cancelled = true;
      };
    }
    async function loadFeatureFlags() {
      try {
        const remote = await getFeatureFlags();
        if (!cancelled) setFeatureFlags(remote);
      } catch {
        if (!cancelled) setFeatureFlags({ enabled_keys: [] });
      }
    }
    void loadFeatureFlags();
    return () => {
      cancelled = true;
    };
  }, [user]);

  useEffect(() => {
    let cancelled = false;
    if (!user) {
      setOnboardingState(null);
      return () => {
        cancelled = true;
      };
    }
    async function loadOnboardingState() {
      try {
        const remote = await getOnboardingState();
        if (!cancelled) setOnboardingState(remote);
      } catch {
        if (!cancelled) setOnboardingState(null);
      }
    }
    void loadOnboardingState();
    return () => {
      cancelled = true;
    };
  }, [user]);

  if (!ready) {
    return (
      <div className="flex h-screen items-center justify-center bg-runtime-bg">
        <div className="flex items-center gap-2 text-sm text-ink-muted">
          <span className="h-1.5 w-1.5 animate-telemetry-pulse rounded-full bg-signal-protocol" />
          Loading...
        </div>
      </div>
    );
  }

  return (
    <DashboardEntrypoint
      user={user}
      authError={authError}
      featureFlags={featureFlags}
      onboardingState={onboardingState}
      onUserChange={setUser}
      onOnboardingStateChange={setOnboardingState}
    />
  );
}

type DashboardEntrypointProps = {
  user: Session | null;
  authError: string | null;
  featureFlags: FeatureFlags;
  onboardingState: OnboardingState | null;
  onUserChange: Dispatch<SetStateAction<Session | null>>;
  onOnboardingStateChange: Dispatch<SetStateAction<OnboardingState | null>>;
};

function DashboardEntrypoint({
  user,
  authError,
  featureFlags,
  onboardingState,
  onUserChange,
  onOnboardingStateChange,
}: DashboardEntrypointProps) {
  if (!user) return <Login onLogin={onUserChange} initialError={authError} />;

  return (
    <AuthenticatedDashboard
      user={user}
      featureFlags={featureFlags}
      onboardingState={onboardingState}
      onUserChange={onUserChange}
      onOnboardingStateChange={onOnboardingStateChange}
    />
  );
}

type AuthenticatedDashboardProps = {
  user: Session;
  featureFlags: FeatureFlags;
  onboardingState: OnboardingState | null;
  onUserChange: Dispatch<SetStateAction<Session | null>>;
  onOnboardingStateChange: Dispatch<SetStateAction<OnboardingState | null>>;
};

const AuthenticatedDashboard = memo(function AuthenticatedDashboard({
  user,
  featureFlags,
  onboardingState,
  onUserChange,
  onOnboardingStateChange,
}: AuthenticatedDashboardProps) {
  useDashboardRenderDiagnostic("dashboard-shell");

  const location = useLocation();
  const navigateRef = useRef<NavigateFunction | null>(null);
  const [commandOpen, setCommandOpen] = useState(false);
  const [commandQuery, setCommandQuery] = useState("");
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false);
  // Mandate A: primary navigation is visible by default on desktop (collapsible, not
  // hidden-by-default). The toggle only collapses it for users who want more width.
  const [desktopSidebarOpen, setDesktopSidebarOpen] = useState(true);
  const commandInputRef = useRef<HTMLInputElement>(null);

  const enabledFeatureFlags = useMemo(
    () => new Set(featureFlags.enabled_keys),
    [featureFlags.enabled_keys],
  );
  const navSections = useMemo(
    () => visibleDashboardNavSections(enabledFeatureFlags),
    [enabledFeatureFlags],
  );
  const commandItems = useMemo(
    () => commandNavigationItems(enabledFeatureFlags),
    [enabledFeatureFlags],
  );
  const workspaceActive = useMemo(
    () => matchDashboardRoute(location.pathname)?.route.id === "workspace",
    [location.pathname],
  );
  const filteredCommands = useMemo(() => {
    return searchCommandNavigationItems(commandItems, commandQuery);
  }, [commandItems, commandQuery]);

  const setRoutePath = useCallback(
    (pathname: string, options: { replace?: boolean; source?: string } = {}) => {
      const nextUrl = pathname.startsWith("/") ? pathname : `/${pathname}`;
      const currentUrl = `${window.location.pathname}${window.location.search}${window.location.hash}`;
      if (currentUrl !== nextUrl) {
        const navigate = navigateRef.current;
        if (navigate) {
          navigate(nextUrl, { replace: Boolean(options.replace) });
        } else {
          window.location.assign(nextUrl);
        }
      }
      const nextPathname = new URL(nextUrl, window.location.origin).pathname;
      const nextMatch = matchDashboardRoute(nextPathname);
      trackEvent("app_route_selected", {
        route: nextMatch?.route.id ?? "unknown",
        path: nextPathname,
        source: options.source ?? (options.replace ? "replace" : "navigation"),
      });
      setCommandOpen(false);
      setMobileMenuOpen(false);
    },
    [],
  );

  const navigateToRoute = useCallback(
    (
      routeId: DashboardRouteId,
      options: { replace?: boolean; source?: string } = {},
    ) => {
      setRoutePath(ROUTE_BY_ID[routeId].path, {
        ...options,
        source: options.source ?? "navigation",
      });
    },
    [setRoutePath],
  );

  const navigateToCommand = useCallback(
    (item: { path: string }) => {
      setRoutePath(item.path, { source: "command_palette" });
    },
    [setRoutePath],
  );

  const routeLinkClick = useCallback(
    (
      event: MouseEvent<HTMLAnchorElement>,
      pathname: string,
      source: string,
    ) => {
      if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) {
        return;
      }
      event.preventDefault();
      setRoutePath(pathname, { source });
    },
    [setRoutePath],
  );

  const signOut = useCallback(async () => {
    trackEvent("auth_logout");
    let upstreamLogoutUrl: string | null | undefined;
    try {
      const result = await logoutSession(currentRedirectPath());
      upstreamLogoutUrl = result.logout_url;
    } finally {
      session.clear();
      onUserChange(null);
    }
    if (upstreamLogoutUrl) {
      window.location.assign(upstreamLogoutUrl);
    }
  }, [onUserChange]);

  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      const key = event.key.toLowerCase();
      if ((event.metaKey || event.ctrlKey) && key === "k") {
        event.preventDefault();
        setCommandOpen((open) => !open);
        return;
      }
      if (key === "escape") setCommandOpen(false);
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  useEffect(() => {
    if (!commandOpen) {
      setCommandQuery("");
      return;
    }
    const id = window.setTimeout(() => commandInputRef.current?.focus(), 0);
    return () => window.clearTimeout(id);
  }, [commandOpen]);

  return (
    <div className="flex h-screen flex-col bg-transparent">
      <DashboardNavigateBinder navigateRef={navigateRef} />
      <DashboardRouteEffects
        enabledFeatureFlags={enabledFeatureFlags}
        onNavigateRoute={navigateToRoute}
      />
      <header className="shrink-0 border-b border-runtime-line-soft bg-runtime-bg/85 shadow-[0_1px_0_rgba(126,231,208,0.05)] backdrop-blur-xl">
        <div className="flex h-12 items-center justify-between gap-3 px-3 sm:px-4">
          <div className="flex min-w-0 items-center gap-2.5">
            <ToolbarButton
              onClick={() => setMobileMenuOpen(true)}
              aria-label="Open navigation menu"
              aria-expanded={mobileMenuOpen}
              variant="secondary"
              size="md"
              className="h-9 w-9 px-0 lg:hidden"
            >
              <Icon name="menu" size={16} />
            </ToolbarButton>
            <ToolbarButton
              onClick={() => setDesktopSidebarOpen((open) => !open)}
              aria-label={desktopSidebarOpen ? "Close navigation" : "Open navigation"}
              aria-expanded={desktopSidebarOpen}
              variant="secondary"
              size="md"
              className="hidden h-9 w-9 px-0 lg:inline-flex"
            >
              <Icon name={desktopSidebarOpen ? "panel-left" : "menu"} size={16} />
            </ToolbarButton>
            <Link
              to={ROUTE_BY_ID.workspace.path}
              onClick={(event) => routeLinkClick(event, ROUTE_BY_ID.workspace.path, "brand")}
              className="-ml-1 flex shrink-0 items-center gap-2.5 rounded-lg px-1 py-1 outline-none transition hover:bg-runtime-panel/70 focus-visible:ring-2 focus-visible:ring-signal-protocol/40"
              title="verified work by agents"
            >
              <img
                src="/brand/logomark.svg"
                alt=""
                className="h-7 w-7 rounded-md shadow-sm shadow-black/30 ring-1 ring-white/10"
              />
              <span className="flex min-w-0 items-baseline gap-2">
                <span className="text-[15px] font-semibold text-ink">a2a</span>
                <span className="hidden text-[11px] font-medium uppercase tracking-wider text-ink-faint lg:inline">
                  Dashboard
                </span>
              </span>
            </Link>
          </div>
          <div className="flex min-w-0 items-center gap-1.5">
            {!workspaceActive && (
              <Link
                to={ROUTE_BY_ID.workspace.path}
                onClick={(event) =>
                  routeLinkClick(event, ROUTE_BY_ID.workspace.path, "return_to_task")
                }
                className="inline-flex h-9 shrink-0 items-center gap-1.5 rounded-lg border border-signal-protocol/35 bg-signal-protocol/10 px-2.5 text-[12px] font-medium text-signal-protocol transition hover:border-signal-protocol/60 hover:bg-signal-protocol/15 focus:outline-none focus-visible:ring-2 focus-visible:ring-signal-protocol/40"
                title="Return to the current workspace task"
              >
                <Icon name="sparkle" size={13} />
                <span className="hidden md:inline">Return to task</span>
              </Link>
            )}
            <ToolbarButton
              onClick={() => setCommandOpen(true)}
              aria-label="Open command palette"
              variant="secondary"
              className="h-9 w-9 border-runtime-line-soft bg-runtime-panel/65 px-0 text-[13px] text-ink-soft shadow-sm shadow-black/20 hover:border-runtime-line hover:bg-runtime-panel sm:w-56 sm:justify-start sm:px-3"
            >
              <Icon name="search" size={16} />
              <span className="hidden min-w-0 flex-1 text-left sm:inline">Search dashboard</span>
              <kbd className="hidden rounded-md border border-runtime-line bg-runtime-bg px-1.5 py-0.5 font-mono text-[10px] text-ink-muted sm:inline">Ctrl K</kbd>
            </ToolbarButton>
            <div className="mx-1 hidden h-5 w-px bg-runtime-line-soft sm:block" />
            <span className="hidden max-w-[200px] truncate text-[13px] text-ink-dim md:inline" title={user.email}>
              {user.email}
            </span>
            <ToolbarButton
              onClick={signOut}
              variant="ghost"
              className="border-transparent text-[13px]"
            >
              Sign out
            </ToolbarButton>
          </div>
        </div>
      </header>

      <div className="flex min-h-0 flex-1">
        {desktopSidebarOpen && (
          <DashboardSidebarWithRouteState
            sections={navSections}
            enabledFeatureFlags={enabledFeatureFlags}
            onLinkClick={routeLinkClick}
            onDismiss={() => setDesktopSidebarOpen(false)}
            className="hidden lg:flex"
          />
        )}
        <main className="flex min-h-0 min-w-0 flex-1 flex-col">
          <DashboardRoutes enabledFeatureFlags={enabledFeatureFlags} />
        </main>
      </div>
      {mobileMenuOpen && (
        <div
          className="fixed inset-0 z-40 bg-black/60 backdrop-blur-sm lg:hidden"
          onMouseDown={() => setMobileMenuOpen(false)}
        >
          <div
            className="h-full w-full max-w-[20rem]"
            onMouseDown={(event) => event.stopPropagation()}
          >
            <DashboardSidebarWithRouteState
              sections={navSections}
              enabledFeatureFlags={enabledFeatureFlags}
              onLinkClick={routeLinkClick}
              onDismiss={() => setMobileMenuOpen(false)}
              dismissOnNavigate
              className="flex h-full shadow-2xl shadow-black/70"
            />
          </div>
        </div>
      )}

      <DashboardCommandPalette
        open={commandOpen}
        query={commandQuery}
        items={filteredCommands}
        enabledFeatureFlags={enabledFeatureFlags}
        inputRef={commandInputRef}
        onQueryChange={setCommandQuery}
        onDismiss={() => setCommandOpen(false)}
        onSubmitItem={navigateToCommand}
        onItemClick={(event, item) => routeLinkClick(event, item.path, "command_palette")}
      />
      {onboardingState && !onboardingState.completed && !onboardingState.dismissed && (
        <OnboardingWizard
          state={onboardingState}
          onStateChange={onOnboardingStateChange}
          onComplete={onOnboardingStateChange}
          onNavigate={(route) => navigateToRoute(route)}
        />
      )}
    </div>
  );
});

type DashboardNavigateBinderProps = {
  navigateRef: MutableRefObject<NavigateFunction | null>;
};

function DashboardNavigateBinder({ navigateRef }: DashboardNavigateBinderProps) {
  const navigate = useNavigate();

  useEffect(() => {
    navigateRef.current = navigate;
    return () => {
      if (navigateRef.current === navigate) {
        navigateRef.current = null;
      }
    };
  }, [navigate, navigateRef]);

  return null;
}

type DashboardRouteEffectsProps = {
  enabledFeatureFlags: Set<string>;
  onNavigateRoute: (
    routeId: DashboardRouteId,
    options?: { replace?: boolean; source?: string },
  ) => void;
};

function DashboardRouteEffects({
  enabledFeatureFlags,
  onNavigateRoute,
}: DashboardRouteEffectsProps) {
  const location = useLocation();
  const currentPathname = location.pathname;
  const routeMatch = useMemo(
    () => matchDashboardRoute(currentPathname),
    [currentPathname],
  );
  const activeRoute = routeMatch?.route ?? ROUTE_BY_ID[DEFAULT_ROUTE_ID];
  const activeRouteEnabled = isRouteEnabled(activeRoute, enabledFeatureFlags);

  useEffect(() => {
    if (!routeMatch || !activeRouteEnabled) {
      onNavigateRoute(DEFAULT_ROUTE_ID, { replace: true, source: "route_guard" });
    }
  }, [activeRouteEnabled, onNavigateRoute, routeMatch]);

  useEffect(() => {
    if (!routeMatch || !activeRouteEnabled) return;
    trackEvent("app_route_viewed", {
      route: activeRoute.id,
      path: currentPathname,
    });
  }, [activeRoute.id, activeRouteEnabled, currentPathname, routeMatch]);

  return null;
}

type DashboardSidebarWithRouteStateProps = Omit<DashboardSidebarProps, "activeItem"> & {
  enabledFeatureFlags: Set<string>;
};

function DashboardSidebarWithRouteState({
  enabledFeatureFlags,
  ...sidebarProps
}: DashboardSidebarWithRouteStateProps) {
  const location = useLocation();
  const activeItem = useMemo(
    () => activeDashboardNavItem(location.pathname, enabledFeatureFlags),
    [enabledFeatureFlags, location.pathname],
  );

  return <DashboardSidebar {...sidebarProps} activeItem={activeItem} />;
}

type DashboardCommandPaletteProps = Omit<
  Parameters<typeof CommandPalette>[0],
  "activeItemId" | "activePath"
> & {
  enabledFeatureFlags: Set<string>;
};

function DashboardCommandPalette({
  enabledFeatureFlags,
  ...paletteProps
}: DashboardCommandPaletteProps) {
  const location = useLocation();
  const activeNavItem = useMemo(
    () => activeDashboardNavItem(location.pathname, enabledFeatureFlags),
    [enabledFeatureFlags, location.pathname],
  );
  const routeMatch = useMemo(
    () => matchDashboardRoute(location.pathname),
    [location.pathname],
  );
  const activeRoute = routeMatch?.route ?? ROUTE_BY_ID[DEFAULT_ROUTE_ID];

  return (
    <CommandPalette
      {...paletteProps}
      activeItemId={activeNavItem?.routeId ?? activeRoute.id}
      activePath={location.pathname}
    />
  );
}

type DashboardSidebarProps = {
  sections: DashboardNavSection[];
  activeItem: DashboardNavItem | null;
  onLinkClick: (
    event: MouseEvent<HTMLAnchorElement>,
    pathname: string,
    source: string,
  ) => void;
  onDismiss?: () => void;
  dismissOnNavigate?: boolean;
  className?: string;
};

function DashboardSidebar({
  sections,
  activeItem,
  onLinkClick,
  onDismiss,
  dismissOnNavigate,
  className,
}: DashboardSidebarProps) {
  const activeItemId = activeItem?.id ?? null;

  return (
    <aside
      className={cx(
        "relative w-60 shrink-0 flex-col border-r border-runtime-line-soft bg-runtime-bg/60",
        className,
      )}
      aria-label="Dashboard navigation"
    >
      <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
        {onDismiss && (
          <ToolbarButton
            type="button"
            variant="ghost"
            size="xs"
            onClick={onDismiss}
            className="absolute right-2 top-2 z-10 h-8 w-8 px-0 lg:hidden"
            aria-label="Close navigation"
          >
            <Icon name="close" size={15} />
          </ToolbarButton>
        )}
        <nav className="min-h-0 flex-1 space-y-3 overflow-y-auto px-2.5 py-3 pt-12 lg:pt-3">
          {sections.map((section) => {
            // Task-local tools live inside the workspace cockpit. The global
            // rail only needs one Workspace entry; the command palette keeps
            // every deep destination searchable.
            const items =
              section.id === "start"
                ? section.items
                    .filter((item) => item.id === "chat")
                    .map((item) => ({ ...item, label: "Workspace" }))
                : section.items;

            return (
              <section key={section.id} aria-labelledby={`dashboard-nav-${section.id}`}>
                <h2
                  id={`dashboard-nav-${section.id}`}
                  className="px-2 pb-1 text-[10px] font-semibold uppercase tracking-wider text-ink-faint"
                >
                  {section.label}
                </h2>
                <div className="space-y-0.5">
                  {items.map((item) => (
                    <DashboardSidebarItem
                      key={item.id}
                      item={item}
                      activeItemId={activeItemId}
                      onLinkClick={onLinkClick}
                      onDismiss={onDismiss}
                      dismissOnNavigate={dismissOnNavigate}
                    />
                  ))}
                </div>
              </section>
            );
          })}
        </nav>
      </div>
    </aside>
  );
}

type DashboardSidebarItemProps = {
  item: DashboardNavItem;
  activeItemId: string | null;
  onLinkClick: (
    event: MouseEvent<HTMLAnchorElement>,
    pathname: string,
    source: string,
  ) => void;
  onDismiss?: () => void;
  dismissOnNavigate?: boolean;
};

function DashboardSidebarItem({
  item,
  activeItemId,
  onLinkClick,
  onDismiss,
  dismissOnNavigate,
}: DashboardSidebarItemProps) {
  const active = activeItemId === item.id;
  const activeDescendant = !active && navItemContainsId(item, activeItemId);

  return (
    <div>
      <Link
        to={item.path}
        data-onboarding-route={item.routeId}
        onClick={(event) => {
          onLinkClick(event, item.path, "sidebar");
          // Mandate A: only the mobile drawer dismisses after navigating; the
          // desktop sidebar stays open so navigation is always visible.
          if (dismissOnNavigate) onDismiss?.();
        }}
        aria-current={active ? "page" : activeDescendant ? "location" : undefined}
        title={item.description}
        className={cx(
          "group flex min-h-8 items-center gap-2 rounded-lg border px-2 py-1.5 text-[13px] font-medium transition focus:outline-none focus-visible:ring-2 focus-visible:ring-signal-protocol/40",
          active
            ? "border-signal-protocol/40 bg-signal-protocol/12 text-signal-protocol shadow-sm shadow-black/20"
            : activeDescendant
              ? "border-runtime-line-soft bg-runtime-panel/70 text-ink-soft"
              : "border-transparent text-ink-muted hover:border-runtime-line-soft hover:bg-runtime-panel/70 hover:text-ink-soft",
        )}
      >
        <span
          aria-hidden="true"
          className={cx(
            "h-1.5 w-1.5 shrink-0 rounded-full transition",
            active
              ? "bg-signal-protocol"
              : activeDescendant
                ? "bg-ink-muted"
                : "bg-runtime-line-mid opacity-0 group-hover:opacity-100",
          )}
        />
        <span className="min-w-0 flex-1 truncate">{item.label}</span>
      </Link>
    </div>
  );
}

function navItemContainsId(item: DashboardNavItem, id: string | null): boolean {
  if (!id) return false;
  return item.id === id || Boolean(item.children?.some((child) => navItemContainsId(child, id)));
}
