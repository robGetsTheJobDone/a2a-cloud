import { useEffect, useRef } from "react";

const DEV_MODE = Boolean(
  (import.meta as ImportMeta & { env?: { DEV?: boolean } }).env?.DEV,
);

type DashboardRenderDiagnosticEntry = {
  commitCount: number;
  mountCount: number;
  unmountCount: number;
  currentMountId: number | null;
  lastCommitAt: number | null;
  lastMountAt: number | null;
  lastUnmountAt: number | null;
};

export type DashboardRenderDiagnostics = {
  sequence: number;
  entries: Record<string, DashboardRenderDiagnosticEntry>;
};

declare global {
  interface Window {
    __A2A_DASHBOARD_RENDER_DIAGNOSTICS__?: DashboardRenderDiagnostics;
  }
}

function diagnosticsEnabled() {
  if (typeof window === "undefined") return false;
  if (DEV_MODE) return true;
  try {
    return window.localStorage.getItem("a2a:dashboard-render-diagnostics") === "1";
  } catch {
    return false;
  }
}

function createDiagnostics(): DashboardRenderDiagnostics {
  return {
    sequence: 0,
    entries: {},
  };
}

function ensureDiagnostics() {
  window.__A2A_DASHBOARD_RENDER_DIAGNOSTICS__ ??= createDiagnostics();
  return window.__A2A_DASHBOARD_RENDER_DIAGNOSTICS__;
}

function ensureEntry(diagnostics: DashboardRenderDiagnostics, id: string) {
  diagnostics.entries[id] ??= {
    commitCount: 0,
    mountCount: 0,
    unmountCount: 0,
    currentMountId: null,
    lastCommitAt: null,
    lastMountAt: null,
    lastUnmountAt: null,
  };
  return diagnostics.entries[id];
}

export function useDashboardRenderDiagnostic(id: string) {
  const mountIdRef = useRef<number | null>(null);

  useEffect(() => {
    if (!diagnosticsEnabled()) return undefined;

    const diagnostics = ensureDiagnostics();
    const entry = ensureEntry(diagnostics, id);
    const mountId = diagnostics.sequence + 1;
    diagnostics.sequence = mountId;
    mountIdRef.current = mountId;
    entry.mountCount += 1;
    entry.currentMountId = mountId;
    entry.lastMountAt = Date.now();

    return () => {
      if (!diagnosticsEnabled()) return;
      const nextDiagnostics = ensureDiagnostics();
      const nextEntry = ensureEntry(nextDiagnostics, id);
      nextEntry.unmountCount += 1;
      if (nextEntry.currentMountId === mountIdRef.current) {
        nextEntry.currentMountId = null;
      }
      nextEntry.lastUnmountAt = Date.now();
    };
  }, [id]);

  useEffect(() => {
    if (!diagnosticsEnabled()) return;
    const diagnostics = ensureDiagnostics();
    const entry = ensureEntry(diagnostics, id);
    entry.commitCount += 1;
    entry.lastCommitAt = Date.now();
  });
}
