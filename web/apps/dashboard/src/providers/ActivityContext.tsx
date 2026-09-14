import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useState,
  type ReactNode,
} from "react";

/**
 * ActivityContext — hoisted run-selection state shared by the workspace
 * Activity subview and the global Activity ledger (mandate B: selection lives in
 * context so navigating between the two surfaces never triggers a state reload,
 * and mandate E: the keep-alive pages can read/write it without unmounting).
 *
 * The selection is intentionally generic: a `selectedRunId` plus an optional
 * `selectedRunSection` (overview / files / payload / timeline / events …). The
 * shape is a string so callers built on differing detail-view enums can share
 * one store. Run "kind" is encoded by the caller into the id namespace when it
 * matters (e.g. workspace activity prefixes "subagent:"/"dag:"/"llm:").
 *
 * The context is safe to consume WITHOUT a provider: `useActivitySelection`
 * falls back to a self-contained local store so existing un-wrapped consumers
 * (the global ledger today) keep working until the Wave-2 sweep wraps them.
 */

export type ActivitySelection = {
  selectedRunId: string | null;
  selectedRunSection: string | null;
  /** Replace the active run selection. Pass `null` to clear. */
  setSelectedRun: (runId: string | null, section?: string | null) => void;
  /** Update only the section of the active run without changing the run id. */
  setSelectedRunSection: (section: string | null) => void;
};

const ActivitySelectionContext = createContext<ActivitySelection | null>(null);

function useActivitySelectionStore(
  initialRunId: string | null = null,
  initialSection: string | null = null,
): ActivitySelection {
  const [selectedRunId, setRunId] = useState<string | null>(initialRunId);
  const [selectedRunSection, setRunSection] = useState<string | null>(initialSection);

  const setSelectedRun = useCallback((runId: string | null, section: string | null = null) => {
    setRunId(runId);
    setRunSection(runId ? section : null);
  }, []);

  const setSelectedRunSection = useCallback((section: string | null) => {
    setRunSection(section);
  }, []);

  return useMemo(
    () => ({ selectedRunId, selectedRunSection, setSelectedRun, setSelectedRunSection }),
    [selectedRunId, selectedRunSection, setSelectedRun, setSelectedRunSection],
  );
}

export function ActivitySelectionProvider({
  children,
  initialRunId,
  initialSection,
}: {
  children: ReactNode;
  initialRunId?: string | null;
  initialSection?: string | null;
}) {
  const value = useActivitySelectionStore(initialRunId ?? null, initialSection ?? null);
  return (
    <ActivitySelectionContext.Provider value={value}>
      {children}
    </ActivitySelectionContext.Provider>
  );
}

/**
 * Read the shared activity selection. When no provider is mounted above the
 * caller, a local fallback store is created so the hook is always safe to call.
 */
export function useActivitySelection(): ActivitySelection {
  const ctx = useContext(ActivitySelectionContext);
  const fallback = useActivitySelectionStore();
  return ctx ?? fallback;
}
