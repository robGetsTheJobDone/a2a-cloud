import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useReducer,
  useRef,
  type Dispatch,
  type ReactNode,
  type SetStateAction,
} from "react";

type DashboardCacheListener = () => void;

type DashboardResourceEntry<T> = {
  data: T | null;
  error: string | null;
  promise: Promise<T> | null;
  loading: boolean;
  refreshing: boolean;
  updatedAt: number | null;
};

type DashboardResourceSnapshot<T> = {
  data: T | null;
  error: string | null;
  loading: boolean;
  refreshing: boolean;
  updatedAt: number | null;
};

type DashboardResourceLoadOptions = {
  force?: boolean;
};

export const DASHBOARD_SECTION_CACHE_KEYS = {
  workspace: {
    threads: "workspace.threads",
    threadQuery: "workspace.threads.query",
    llmCreds: "workspace.llm-creds",
    composerDraft: "workspace.composer.draft",
    filesAll: "workspace.files.all",
    fileBrowserEntries: "workspace.file-browser.entries",
    fileBrowserExpanded: "workspace.file-browser.expanded",
    activityDetailPrefix: "workspace.activity-detail",
  },
  operate: {
    activityListPrefix: "operate.activity.list",
    activitySourceFilter: "operate.activity.source-filter",
    activityStatusFilter: "operate.activity.status-filter",
    activityQuery: "operate.activity.query",
    schedules: "operate.schedules",
    scheduleDraft: "operate.schedules.draft",
    trialRooms: "operate.trial-rooms",
  },
  agents: {
    marketplace: "agents.marketplace",
    composeCandidates: "agents.compose.candidates",
    composeQuery: "agents.compose.query",
    composeSelected: "agents.compose.selected",
    composeDraft: "agents.compose.draft",
    installedSetup: "agents.installed-setup",
    bounties: "agents.bounties",
    myAgents: "agents.my-agents",
    myAgentsFilters: "agents.my-agents.filters",
    myAgentsSelectedName: "agents.my-agents.selected-agent-name",
  },
  runtime: {
    controlRoom: "runtime.control-room",
    controlRoomPolicyDraft: "runtime.control-room.policy-draft",
    controlRoomSource: "runtime.control-room.source",
    controlReceiptPrefix: "runtime.control-receipt",
    simulations: "runtime.simulations",
    simulationSelectedTemplate: "runtime.simulations.selected-template",
    simulationBuilderKind: "runtime.simulations.builder-kind",
    simulationBuilderTitle: "runtime.simulations.builder-title",
    simulationBuilderGoal: "runtime.simulations.builder-goal",
    simulationExecutionMode: "runtime.simulations.execution-mode",
    simulationSelectedAgents: "runtime.simulations.selected-agents",
    simulationSpecText: "runtime.simulations.spec-text",
    simulationEvolutionTitle: "runtime.simulations.evolution-title",
    simulationEvolutionSeed: "runtime.simulations.evolution-seed",
    simulationEvolutionVariantCount: "runtime.simulations.evolution-variant-count",
    simulationEvolutionMaxDelta: "runtime.simulations.evolution-max-delta",
    simulationEvolutionParticipants: "runtime.simulations.evolution-participants",
  },
  settings: {
    access: "settings.access",
    llmKeyCreds: "settings.llm-keys.creds",
    organizationList: "settings.organization.list",
    organizationDetailsPrefix: "settings.organization.details",
    complianceStatusPrefix: "settings.compliance.status",
    complianceAgentsPrefix: "settings.compliance.agents",
  },
} as const;

export function dashboardScopedCacheKey(
  prefix: string,
  ...parts: Array<string | number | null | undefined>
) {
  const suffix = parts
    .map((part) => String(part ?? "none"))
    .join(":");
  return suffix ? `${prefix}:${suffix}` : prefix;
}

function emptyResourceEntry<T>(): DashboardResourceEntry<T> {
  return {
    data: null,
    error: null,
    promise: null,
    loading: false,
    refreshing: false,
    updatedAt: null,
  };
}

function resourceSnapshot<T>(
  entry: DashboardResourceEntry<T> | undefined,
): DashboardResourceSnapshot<T> {
  const current = entry ?? emptyResourceEntry<T>();
  return {
    data: current.data,
    error: current.error,
    loading: current.loading,
    refreshing: current.refreshing,
    updatedAt: current.updatedAt,
  };
}

function formatResourceError(error: unknown) {
  return error instanceof Error ? error.message : String(error);
}

class DashboardSectionCache {
  private resources = new Map<string, DashboardResourceEntry<unknown>>();
  private values = new Map<string, unknown>();
  private listeners = new Set<DashboardCacheListener>();

  subscribe(listener: DashboardCacheListener) {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  }

  snapshot<T>(key: string): DashboardResourceSnapshot<T> {
    return resourceSnapshot(this.resources.get(key) as DashboardResourceEntry<T> | undefined);
  }

  load<T>(
    key: string,
    loader: () => Promise<T>,
    options: DashboardResourceLoadOptions = {},
  ): Promise<T> {
    const current = this.resources.get(key) as DashboardResourceEntry<T> | undefined;
    if (!options.force) {
      if (current?.data !== null && current?.data !== undefined) {
        return current.promise ?? Promise.resolve(current.data);
      }
      if (current?.promise) return current.promise;
    }

    const previous = current ?? emptyResourceEntry<T>();
    const nextEntry: DashboardResourceEntry<T> = {
      data: previous.data,
      error: null,
      promise: null,
      loading: previous.data === null,
      refreshing: previous.data !== null,
      updatedAt: previous.updatedAt,
    };

    const promise = loader().then(
      (data) => {
        this.resources.set(key, {
          data,
          error: null,
          promise: null,
          loading: false,
          refreshing: false,
          updatedAt: Date.now(),
        });
        this.notify();
        return data;
      },
      (error: unknown) => {
        this.resources.set(key, {
          data: nextEntry.data,
          error: formatResourceError(error),
          promise: null,
          loading: false,
          refreshing: false,
          updatedAt: nextEntry.updatedAt,
        });
        this.notify();
        throw error;
      },
    );

    nextEntry.promise = promise;
    this.resources.set(key, nextEntry);
    this.notify();
    return promise;
  }

  set<T>(key: string, value: SetStateAction<T | null>) {
    const current = this.snapshot<T>(key);
    const nextData =
      typeof value === "function"
        ? (value as (currentValue: T | null) => T | null)(current.data)
        : value;
    this.resources.set(key, {
      data: nextData,
      error: null,
      promise: null,
      loading: false,
      refreshing: false,
      updatedAt: Date.now(),
    });
    this.notify();
  }

  value<T>(key: string, initialValue: T | (() => T)): T {
    if (!this.values.has(key)) {
      this.values.set(
        key,
        typeof initialValue === "function"
          ? (initialValue as () => T)()
          : initialValue,
      );
    }
    return this.values.get(key) as T;
  }

  setValue<T>(key: string, value: SetStateAction<T>) {
    const current = this.values.get(key) as T | undefined;
    const nextValue =
      typeof value === "function"
        ? (value as (currentValue: T) => T)(current as T)
        : value;
    this.values.set(key, nextValue);
    this.notify();
  }

  private notify() {
    for (const listener of this.listeners) listener();
  }
}

export function createDashboardSectionCache() {
  return new DashboardSectionCache();
}

const DashboardSectionCacheContext = createContext<DashboardSectionCache | null>(null);

export function DashboardSectionCacheProvider({
  children,
}: {
  children: ReactNode;
}) {
  const cache = useMemo(() => createDashboardSectionCache(), []);
  return (
    <DashboardSectionCacheContext.Provider value={cache}>
      {children}
    </DashboardSectionCacheContext.Provider>
  );
}

function useDashboardSectionCache() {
  const cache = useContext(DashboardSectionCacheContext);
  if (!cache) {
    throw new Error("Dashboard section cache is missing a provider");
  }
  return cache;
}

export function useDashboardSectionResource<T>(
  key: string,
  loader: () => Promise<T>,
  options: { enabled?: boolean } = {},
) {
  const cache = useDashboardSectionCache();
  const enabled = options.enabled ?? true;
  const loaderRef = useRef(loader);
  const [version, bumpVersion] = useReducer((value: number) => value + 1, 0);

  useEffect(() => {
    loaderRef.current = loader;
  }, [loader]);

  useEffect(() => cache.subscribe(bumpVersion), [cache]);

  useEffect(() => {
    if (!enabled) return;
    void cache.load(key, () => loaderRef.current()).catch(() => undefined);
  }, [cache, enabled, key]);

  const snapshot = useMemo(
    () => cache.snapshot<T>(key),
    [cache, key, version],
  );

  const refresh = useCallback(() => {
    if (!enabled) return Promise.resolve(undefined);
    return cache.load(key, () => loaderRef.current(), { force: true });
  }, [cache, enabled, key]);

  const setData = useCallback(
    (value: SetStateAction<T | null>) => cache.set<T>(key, value),
    [cache, key],
  );

  return {
    ...snapshot,
    refresh,
    setData,
  };
}

export function useDashboardSectionState<T>(
  key: string,
  initialValue: T | (() => T),
): [T, Dispatch<SetStateAction<T>>] {
  const cache = useDashboardSectionCache();
  const initialValueRef = useRef(initialValue);
  const [version, bumpVersion] = useReducer((value: number) => value + 1, 0);

  useEffect(() => cache.subscribe(bumpVersion), [cache]);

  const value = useMemo(
    () => cache.value<T>(key, initialValueRef.current),
    [cache, key, version],
  );

  const setValue = useCallback(
    (nextValue: SetStateAction<T>) => cache.setValue<T>(key, nextValue),
    [cache, key],
  );

  return [value, setValue];
}
