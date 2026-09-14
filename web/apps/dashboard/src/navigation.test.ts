import { describe, expect, it } from "vitest";
import {
  DASHBOARD_NAV_SECTIONS,
  activeDashboardNavItem,
  commandNavigationItems,
  flattenDashboardNavItems,
  runtimeControlReceiptHref,
  searchCommandNavigationItems,
  visibleDashboardNavSections,
} from "./navigation";

const ENABLED_NAV_FLAGS = new Set([
  "dashboard.simulations",
  "dashboard.bounties",
]);

const UNGATED_SIDEBAR_PATHS = [
  "/workspace",
  "/workspace/threads",
  "/workspace/files",
  "/workspace/artifacts",
  "/workspace/settings",
  "/workspace/activity",
  "/activity",
  "/trials",
  "/schedules",
  "/schedules/list",
  "/schedules/create",
  "/studio",
  "/my-agents",
  "/marketplace",
  "/marketplace/proofs",
  "/compose",
  "/installed-setup",
  "/runtime",
  "/runtime/timeline",
  "/runtime/policy",
  "/access",
  "/access/langfuse",
  "/access/litellm",
  "/access/gitea",
  "/access/repositories",
  "/llm-keys",
  "/llm-keys/saved",
  "/llm-keys/add",
  "/organization",
] as const;

function visiblePaths(flags = new Set<string>()) {
  return flattenDashboardNavItems(visibleDashboardNavSections(flags)).map((item) => item.path);
}

describe("dashboard sidebar navigation", () => {
  it("builds runtime receipt routes from source receipt paths", () => {
    expect(runtimeControlReceiptHref("subagent/grant-1")).toBe(
      "/runtime/timeline/receipts/c3ViYWdlbnQvZ3JhbnQtMQ",
    );
    expect(runtimeControlReceiptHref("dag/dag-1", "trace")).toBe(
      "/runtime/timeline/receipts/ZGFnL2RhZy0x/trace",
    );
  });

  it("includes every ungated ID-free route-level destination", () => {
    const paths = visiblePaths();

    expect(paths).toEqual(expect.arrayContaining([...UNGATED_SIDEBAR_PATHS]));
  });

  it("leads the Build section with Studio, the shortest path to a deployed agent", () => {
    const build = DASHBOARD_NAV_SECTIONS.find((section) => section.label === "Build");

    expect(build?.items[0]?.routeId).toBe("studio");
    expect(build?.items[0]?.path).toBe("/studio");
  });

  it("hides feature-gated nav items when flags are disabled", () => {
    const disabledPaths = visiblePaths();
    const enabledPaths = visiblePaths(ENABLED_NAV_FLAGS);

    expect(disabledPaths).not.toContain("/simulations");
    expect(disabledPaths).not.toContain("/bounties/open");
    expect(enabledPaths).toContain("/simulations");
    expect(enabledPaths).toContain("/bounties/open");
  });

  it("does not reuse the old top chrome category labels", () => {
    const labels = visibleDashboardNavSections(new Set()).map((section) => section.label);

    expect(labels).not.toContain("Work");
    expect(labels).not.toContain("Workspace");
    expect(labels).not.toContain("Agents");
    expect(labels).not.toContain("Admin");
  });

  it("selects the deepest matching sidebar item for active routes", () => {
    expect(activeDashboardNavItem("/runtime/policy/budgets", new Set())?.id)
      .toBe("runtime-policy");
    expect(activeDashboardNavItem("/llm-keys/saved/default/usage", new Set())?.id)
      .toBe("llm-keys-saved");
    expect(activeDashboardNavItem("/workspace/files/file/src/App.tsx", new Set())?.id)
      .toBe("files");
    expect(activeDashboardNavItem("/marketplace/proofs", new Set())?.id)
      .toBe("marketplace-proofs");
  });

  it("generates command palette destinations from the same sidebar tree", () => {
    const navPaths = visiblePaths(ENABLED_NAV_FLAGS);
    const commandPaths = commandNavigationItems(ENABLED_NAV_FLAGS).map((item) => item.path);

    expect(commandPaths).toEqual(navPaths);
  });
});

describe("commandNavigationItems", () => {
  it("returns unique command paths", () => {
    const paths = commandNavigationItems(ENABLED_NAV_FLAGS).map((item) => item.path);

    expect(new Set(paths).size).toBe(paths.length);
  });

  it("ranks exact paths ahead of broader command matches", () => {
    const items = commandNavigationItems(new Set());
    const matches = searchCommandNavigationItems(items, "/access/repositories");

    expect(matches[0]?.path).toBe("/access/repositories");
  });

  it("reaches Studio from the palette with no feature flags enabled", () => {
    const items = commandNavigationItems(new Set());

    expect(items.map((item) => item.path)).toContain("/studio");
    expect(searchCommandNavigationItems(items, "describe an agent")[0]?.path)
      .toBe("/studio");
    expect(searchCommandNavigationItems(items, "studio")[0]?.path).toBe("/studio");
  });

  it("finds dashboard tasks from user language", () => {
    const items = commandNavigationItems(new Set());

    expect(searchCommandNavigationItems(items, "provider key")[0]?.path).toBe("/llm-keys");
    expect(searchCommandNavigationItems(items, "budget caps")[0]?.path)
      .toBe("/runtime/policy");
    expect(searchCommandNavigationItems(items, "gateway keys")[0]?.path)
      .toBe("/access/litellm");
  });

  it("keeps unavailable feature-flagged command matches out of search", () => {
    const items = commandNavigationItems(new Set());
    const paths = searchCommandNavigationItems(items, "evolution").map((item) => item.path);

    expect(paths).not.toContain("/simulations");
    expect(paths).not.toContain("/simulations/evolution");
    expect(paths).not.toContain("/bounties/open");
  });
});
