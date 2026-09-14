import { describe, expect, it } from "vitest";
import {
  DASHBOARD_SECTION_CACHE_KEYS,
  createDashboardSectionCache,
  dashboardScopedCacheKey,
} from "./DashboardSectionCache";

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((promiseResolve, promiseReject) => {
    resolve = promiseResolve;
    reject = promiseReject;
  });
  return { promise, resolve, reject };
}

describe("DashboardSectionCache", () => {
  it("keeps cache key namespaces aligned with persistent sections", () => {
    const entries = Object.entries(DASHBOARD_SECTION_CACHE_KEYS);

    expect(entries.map(([section]) => section).sort()).toEqual([
      "agents",
      "operate",
      "runtime",
      "settings",
      "workspace",
    ]);

    for (const [section, keys] of entries) {
      for (const key of Object.values(keys)) {
        expect(key.startsWith(`${section}.`)).toBe(true);
      }
    }
  });

  it("builds dynamic cache keys inside their section namespace", () => {
    expect(dashboardScopedCacheKey("workspace.activity-detail", "thread-1", "dag", "job-1"))
      .toBe("workspace.activity-detail:thread-1:dag:job-1");
    expect(DASHBOARD_SECTION_CACHE_KEYS.workspace.composerDraft)
      .toBe("workspace.composer.draft");
    expect(dashboardScopedCacheKey(DASHBOARD_SECTION_CACHE_KEYS.runtime.controlReceiptPrefix, "receipts/run-1.json"))
      .toBe("runtime.control-receipt:receipts/run-1.json");
    expect(dashboardScopedCacheKey("settings.organization.details", null))
      .toBe("settings.organization.details:none");
  });

  it("deduplicates in-flight resource loads", async () => {
    const cache = createDashboardSectionCache();
    const request = deferred<string>();
    let calls = 0;

    const first = cache.load("settings.keys", () => {
      calls += 1;
      return request.promise;
    });
    const second = cache.load("settings.keys", () => {
      calls += 1;
      return Promise.resolve("unexpected");
    });

    expect(calls).toBe(1);
    expect(cache.snapshot<string>("settings.keys").loading).toBe(true);

    request.resolve("loaded");
    await expect(first).resolves.toBe("loaded");
    await expect(second).resolves.toBe("loaded");
    expect(cache.snapshot<string>("settings.keys").data).toBe("loaded");
  });

  it("uses cached data until a refresh is explicitly forced", async () => {
    const cache = createDashboardSectionCache();
    let calls = 0;

    await cache.load("operate.schedules", async () => {
      calls += 1;
      return ["first"];
    });
    await cache.load("operate.schedules", async () => {
      calls += 1;
      return ["second"];
    });

    expect(calls).toBe(1);
    expect(cache.snapshot<string[]>("operate.schedules").data).toEqual(["first"]);
  });

  it("keeps stale data visible while a forced refresh is pending", async () => {
    const cache = createDashboardSectionCache();
    const request = deferred<string>();

    await cache.load("runtime.timeline", async () => "stale");
    const refresh = cache.load("runtime.timeline", () => request.promise, { force: true });

    expect(cache.snapshot<string>("runtime.timeline")).toMatchObject({
      data: "stale",
      loading: false,
      refreshing: true,
      error: null,
    });

    request.resolve("fresh");
    await expect(refresh).resolves.toBe("fresh");
    expect(cache.snapshot<string>("runtime.timeline")).toMatchObject({
      data: "fresh",
      loading: false,
      refreshing: false,
      error: null,
    });
  });

  it("keeps section state values until the section cache is replaced", () => {
    const cache = createDashboardSectionCache();

    expect(cache.value("workspace.composer", "draft one")).toBe("draft one");
    cache.setValue<string>("workspace.composer", (current) => `${current} updated`);

    expect(cache.value("workspace.composer", "draft two")).toBe("draft one updated");
    expect(createDashboardSectionCache().value("workspace.composer", "draft two"))
      .toBe("draft two");
  });
});
