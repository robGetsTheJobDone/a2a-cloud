import { Component, type ErrorInfo, type ReactNode } from "react";
import { SurfacePanel, ToolbarLink } from "../DashboardChrome";
import { StatusBadge } from "../StatusPillAdapters";
import { Icon } from "../Icon";
import { workspaceActivityHref } from "../../navigation";

/**
 * Workspace chat error recovery. The old WorkspaceChatConsoleShell header +
 * thread-peek panel were removed in the single-row chrome pass — workspace
 * navigation now lives solely in the WorkspacePage posture strip and the Chat
 * header — so this module only carries the failure boundary/notice that wrap
 * the chat subtree.
 */
type WorkspaceChatFailureBoundaryProps = {
  resetKey: string;
  threadId: string | null;
  children: ReactNode;
};

type WorkspaceChatFailureBoundaryState = {
  errorMessage: string | null;
};

export class WorkspaceChatFailureBoundary extends Component<
  WorkspaceChatFailureBoundaryProps,
  WorkspaceChatFailureBoundaryState
> {
  state: WorkspaceChatFailureBoundaryState = { errorMessage: null };

  static getDerivedStateFromError(error: unknown): WorkspaceChatFailureBoundaryState {
    return { errorMessage: formatWorkspaceChatError(error) };
  }

  componentDidCatch(error: unknown, info: ErrorInfo) {
    console.error("Workspace chat failed to render", error, info);
  }

  componentDidUpdate(previousProps: WorkspaceChatFailureBoundaryProps) {
    if (
      previousProps.resetKey !== this.props.resetKey &&
      this.state.errorMessage
    ) {
      this.setState({ errorMessage: null });
    }
  }

  render() {
    if (this.state.errorMessage) {
      return (
        <WorkspaceChatFailureNotice
          threadId={this.props.threadId}
          errorMessage={this.state.errorMessage}
        />
      );
    }

    return this.props.children;
  }
}

export function WorkspaceChatFailureNotice({
  threadId,
  errorMessage,
}: {
  threadId: string | null;
  errorMessage: string;
}) {
  const settingsHref = threadId
    ? `/workspace/settings?thread=${encodeURIComponent(threadId)}`
    : "/workspace/settings";

  return (
    <div className="flex h-full min-h-0 items-center justify-center overflow-auto bg-runtime-bg p-4 sm:p-6">
      <SurfacePanel
        as="section"
        role="alert"
        className="w-full max-w-2xl bg-runtime-bg p-5"
      >
        <StatusBadge tone="red" dot>
          chat unavailable
        </StatusBadge>
        <h2 className="mt-3 text-lg font-semibold text-ink">
          Workspace chat could not render
        </h2>
        <p className="mt-2 text-sm leading-relaxed text-ink-muted">
          The workspace route is still available. Start a new chat, inspect activity, or adjust thread settings while this panel recovers.
        </p>
        <div className="mt-4 rounded-md border border-signal-danger/50 bg-signal-danger/12 p-3 font-mono text-xs text-signal-danger [overflow-wrap:anywhere]">
          {errorMessage}
        </div>
        <div className="mt-4 flex flex-wrap gap-2">
          <ToolbarLink href="/workspace" variant="primary">
            <Icon name="plus" size={14} />
            New chat
          </ToolbarLink>
          <ToolbarLink href={workspaceActivityHref(threadId)}>
            <Icon name="panel-right" size={14} />
            Activity
          </ToolbarLink>
          <ToolbarLink href={settingsHref}>
            <Icon name="sliders" size={14} />
            Settings
          </ToolbarLink>
        </div>
      </SurfacePanel>
    </div>
  );
}

function formatWorkspaceChatError(error: unknown) {
  if (error instanceof Error && error.message) return error.message;
  return String(error || "Unknown workspace chat error");
}
