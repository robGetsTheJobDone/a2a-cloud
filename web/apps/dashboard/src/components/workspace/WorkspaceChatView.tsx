import { useEffect, useRef } from "react";
import { useLocation, useSearchParams } from "react-router-dom";
import { Chat } from "../Chat";
import { ChatActivityRail } from "../ChatActivityRail";
import { ToolbarButton } from "../DashboardChrome";
import { FileBrowser } from "../FileBrowser";
import { Icon } from "../Icon";
import { DetailSheet } from "../ListDetailLayout";
import { WorkspaceArtifactPreviewPanel } from "../WorkspaceArtifactPreview";
import {
  useWorkspaceInspector,
  type WorkspaceInspectorPanel,
} from "../../providers/WorkspaceInspectorContext";
import {
  workspaceArtifactPathFromPathname,
  type WorkspaceViewId,
} from "../../navigation";
import { WorkspaceChatFailureBoundary } from "./WorkspaceChatConsoleShell";

/**
 * WorkspaceChatView is the responsive task cockpit. Chat stays mounted in the
 * center while task-local Files and Activity open as a right inspector on wide
 * screens or a contextual sheet on mobile. File/run details layer over this
 * workbench, so inspection never ejects the user from the active conversation.
 */
export function WorkspaceChatView({
  legacyView = "chat",
}: {
  legacyView?: WorkspaceViewId;
}) {
  const location = useLocation();
  const [searchParams] = useSearchParams();
  const threadId = searchParams.get("thread") || null;
  const inspector = useWorkspaceInspector();
  const routedArtifactPath =
    workspaceArtifactPathFromPathname(location.pathname, "files") ??
    workspaceArtifactPathFromPathname(location.pathname, "artifacts");
  const hydratedArtifactPathRef = useRef<string | null>(null);

  useEffect(() => {
    const requestedPanel = panelForLegacyView(legacyView);
    if (requestedPanel) inspector.openPanel(requestedPanel);
  }, [inspector.openPanel, legacyView]);

  useEffect(() => {
    if (routedArtifactPath && hydratedArtifactPathRef.current !== routedArtifactPath) {
      hydratedArtifactPathRef.current = routedArtifactPath;
      inspector.openArtifact({ path: routedArtifactPath });
    } else if (!routedArtifactPath) {
      hydratedArtifactPathRef.current = null;
    }
  }, [inspector.openArtifact, routedArtifactPath]);

  return (
    <div className="relative flex h-full min-h-0 overflow-hidden rounded-xl border border-runtime-line-soft/80 bg-runtime-bg/95 shadow-2xl shadow-black/20">
      <div
        data-onboarding-target="workspace-chat"
        className="min-h-0 min-w-0 flex-1 overflow-hidden"
      >
        <WorkspaceChatFailureBoundary
          resetKey={`${location.pathname}${location.search}`}
          threadId={threadId}
        >
          <Chat
            filesOpen={inspector.panel === "files"}
            onFilesOpenChange={(open) =>
              open ? inspector.openPanel("files") : inspector.closePanel()
            }
            activityOpen={inspector.panel === "activity"}
            onActivityOpenChange={(open) =>
              open ? inspector.openPanel("activity") : inspector.closePanel()
            }
            initialThreadsOpen={legacyView === "threads" ? true : undefined}
            initialSettingsOpen={legacyView === "settings"}
          />
        </WorkspaceChatFailureBoundary>
      </div>

      {inspector.panel && (
        <>
          <button
            type="button"
            className="fixed inset-0 z-20 bg-runtime-bg/75 backdrop-blur-sm lg:hidden"
            aria-label="Close task inspector"
            onClick={inspector.closePanel}
          />
          <aside
            aria-label="Task inspector"
            className="fixed inset-x-2 bottom-2 top-[4.25rem] z-30 flex min-h-0 flex-col overflow-hidden rounded-xl border border-runtime-line bg-runtime-bg shadow-2xl shadow-black/70 lg:static lg:z-auto lg:w-[min(27rem,34vw)] lg:shrink-0 lg:rounded-none lg:border-y-0 lg:border-r-0 lg:shadow-none 2xl:w-[27rem]"
          >
            <WorkspaceInspectorHeader
              panel={inspector.panel}
              onSelect={inspector.openPanel}
              onClose={inspector.closePanel}
            />
            <div className="min-h-0 flex-1 overflow-hidden">
              {inspector.panel === "files" ? (
                <div data-onboarding-target="workspace-files" className="h-full">
                  <FileBrowser
                    selectedPath={inspector.selectedArtifact?.path ?? null}
                    onSelectFile={inspector.openArtifact}
                    onClearSelection={inspector.closeArtifact}
                  />
                </div>
              ) : (
                <ChatActivityRail
                  threadId={threadId}
                  limit={24}
                  variant="embedded"
                  className="h-full"
                />
              )}
            </div>
          </aside>
        </>
      )}

      <DetailSheet
        open={Boolean(inspector.selectedArtifact)}
        onClose={inspector.closeArtifact}
        size="xl"
        title={
          <span className="truncate font-mono text-sm text-ink">
            {inspector.selectedArtifact?.path.split("/").pop() || "File preview"}
          </span>
        }
        description={
          inspector.selectedArtifact ? (
            <span className="font-mono text-[11px] [overflow-wrap:anywhere]">
              {inspector.selectedArtifact.path}
            </span>
          ) : undefined
        }
      >
        {inspector.selectedArtifact && (
          <WorkspaceArtifactPreviewPanel
            artifact={inspector.selectedArtifact}
            className="min-h-0 border-0"
          />
        )}
      </DetailSheet>
    </div>
  );
}

function panelForLegacyView(view: WorkspaceViewId): WorkspaceInspectorPanel | null {
  if (view === "files" || view === "artifacts") return "files";
  if (view === "activity") return "activity";
  return null;
}

function WorkspaceInspectorHeader({
  panel,
  onSelect,
  onClose,
}: {
  panel: WorkspaceInspectorPanel;
  onSelect: (panel: WorkspaceInspectorPanel) => void;
  onClose: () => void;
}) {
  return (
    <div className="flex h-12 shrink-0 items-center justify-between gap-2 border-b border-runtime-line-soft/70 bg-runtime-panel/70 px-2.5">
      <div className="flex min-w-0 items-center gap-1" role="tablist" aria-label="Task context">
        <InspectorTab
          active={panel === "activity"}
          label="Activity"
          icon="panel-right"
          onClick={() => onSelect("activity")}
        />
        <InspectorTab
          active={panel === "files"}
          label="Files & outputs"
          icon="folder"
          onClick={() => onSelect("files")}
        />
      </div>
      <ToolbarButton
        onClick={onClose}
        variant="ghost"
        size="xs"
        className="h-8 w-8 shrink-0 px-0"
        aria-label="Close task inspector"
        title="Close inspector"
      >
        <Icon name="close" size={14} />
      </ToolbarButton>
    </div>
  );
}

function InspectorTab({
  active,
  label,
  icon,
  onClick,
}: {
  active: boolean;
  label: string;
  icon: "panel-right" | "folder";
  onClick: () => void;
}) {
  return (
    <ToolbarButton
      role="tab"
      aria-selected={active}
      active={active}
      variant={active ? "primary" : "ghost"}
      size="sm"
      onClick={onClick}
      className="h-8"
    >
      <Icon name={icon} size={13} />
      {label}
    </ToolbarButton>
  );
}
