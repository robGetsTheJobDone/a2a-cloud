import { useLocation } from "react-router-dom";
import { FullHeightRouteFrame } from "../../components/DashboardChrome";
import { workspaceViewForPath } from "../../navigation";
import { ActivitySelectionProvider } from "../../providers/ActivityContext";
import {
  WorkspaceInspectorProvider,
  type WorkspaceInspectorPanel,
} from "../../providers/WorkspaceInspectorContext";
import { WorkspaceChatView } from "../../components/workspace/WorkspaceChatView";

// Public re-export — keep the import surface other modules/tests depend on
// stable after the extraction into components/workspace/.
export { WorkspaceChatFailureNotice } from "../../components/workspace/WorkspaceChatConsoleShell";

/**
 * WorkspacePage is the persistent task workbench. The transcript and composer
 * never disappear: task-local destinations hydrate an inline inspector rather
 * than swapping the user onto another page. Legacy workspace paths remain valid
 * deep links, but they now open Files or Activity beside the active task.
 */
export function WorkspacePage() {
  const location = useLocation();
  const view = workspaceViewForPath(location.pathname);
  const initialInspector = inspectorForWorkspaceView(view.id);

  return (
    <FullHeightRouteFrame>
      <ActivitySelectionProvider>
        <WorkspaceInspectorProvider initialPanel={initialInspector}>
          <div className="h-full min-h-0 overflow-hidden p-1.5 sm:p-2">
            <WorkspaceChatView legacyView={view.id} />
          </div>
        </WorkspaceInspectorProvider>
      </ActivitySelectionProvider>
    </FullHeightRouteFrame>
  );
}

function inspectorForWorkspaceView(
  view: ReturnType<typeof workspaceViewForPath>["id"],
): WorkspaceInspectorPanel | null {
  if (view === "files" || view === "artifacts") return "files";
  if (view === "activity") return "activity";
  return null;
}
