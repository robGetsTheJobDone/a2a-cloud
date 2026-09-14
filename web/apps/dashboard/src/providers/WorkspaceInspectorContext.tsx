import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import type { WorkspaceArtifact } from "../components/WorkspaceArtifactPreview";

export type WorkspaceInspectorPanel = "files" | "activity";

export type WorkspaceInspectorValue = {
  panel: WorkspaceInspectorPanel | null;
  openPanel: (panel: WorkspaceInspectorPanel) => void;
  closePanel: () => void;
  togglePanel: (panel: WorkspaceInspectorPanel) => void;
  selectedArtifact: WorkspaceArtifact | null;
  openArtifact: (artifact: WorkspaceArtifact) => void;
  closeArtifact: () => void;
};

export type WorkspaceInspectorProviderProps = {
  children: ReactNode;
  initialPanel?: WorkspaceInspectorPanel | null;
};

const WorkspaceInspectorContext =
  createContext<WorkspaceInspectorValue | null>(null);

export function WorkspaceInspectorProvider({
  children,
  initialPanel = null,
}: WorkspaceInspectorProviderProps) {
  const [panel, setPanel] = useState<WorkspaceInspectorPanel | null>(initialPanel);
  const [selectedArtifact, setSelectedArtifact] =
    useState<WorkspaceArtifact | null>(null);

  const openPanel = useCallback((nextPanel: WorkspaceInspectorPanel) => {
    setPanel(nextPanel);
  }, []);

  const closePanel = useCallback(() => {
    setPanel(null);
  }, []);

  const togglePanel = useCallback((nextPanel: WorkspaceInspectorPanel) => {
    setPanel((currentPanel) =>
      currentPanel === nextPanel ? null : nextPanel,
    );
  }, []);

  const openArtifact = useCallback((artifact: WorkspaceArtifact) => {
    setSelectedArtifact(artifact);
    setPanel("files");
  }, []);

  const closeArtifact = useCallback(() => {
    setSelectedArtifact(null);
  }, []);

  const value = useMemo<WorkspaceInspectorValue>(
    () => ({
      panel,
      openPanel,
      closePanel,
      togglePanel,
      selectedArtifact,
      openArtifact,
      closeArtifact,
    }),
    [
      panel,
      openPanel,
      closePanel,
      togglePanel,
      selectedArtifact,
      openArtifact,
      closeArtifact,
    ],
  );

  return (
    <WorkspaceInspectorContext.Provider value={value}>
      {children}
    </WorkspaceInspectorContext.Provider>
  );
}

export function useOptionalWorkspaceInspector(): WorkspaceInspectorValue | null {
  return useContext(WorkspaceInspectorContext);
}

export function useWorkspaceInspector(): WorkspaceInspectorValue {
  const inspector = useOptionalWorkspaceInspector();
  if (!inspector) {
    throw new Error(
      "useWorkspaceInspector must be used within a WorkspaceInspectorProvider",
    );
  }
  return inspector;
}
