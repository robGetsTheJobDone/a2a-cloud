import { renderToStaticMarkup } from "react-dom/server";
import { StaticRouter } from "react-router-dom/server";
import { describe, expect, it } from "vitest";
import { DashboardSectionCacheProvider } from "../../components/DashboardSectionCache";
import { WorkspacePage, WorkspaceChatFailureNotice } from "./WorkspacePage";

function renderWorkspaceMarkup(node: JSX.Element, location = "/workspace") {
  return renderToStaticMarkup(
    <StaticRouter location={location}>
      <DashboardSectionCacheProvider>
        {node}
      </DashboardSectionCacheProvider>
    </StaticRouter>,
  );
}

describe("Workspace chat console", () => {
  it("keeps the chat workbench visible while a task inspector is open", () => {
    const html = renderWorkspaceMarkup(<WorkspacePage />, "/workspace/activity");

    expect(html).toContain('aria-label="Task inspector"');
    expect(html).toContain('aria-label="Task context"');
    expect(html).toContain("current task");
    expect(html).toContain("Your tasks");
    expect(html).not.toContain('aria-label="Workspace views"');
    expect(html).not.toContain("Chat console");
    expect(html).not.toContain('aria-controls="workspace-thread-preview"');
    expect(html).not.toContain("Show recent workspace threads");
  });

  it("renders a clear recovery state when chat fails", () => {
    const html = renderWorkspaceMarkup(
      <WorkspaceChatFailureNotice
        threadId="thread with spaces"
        errorMessage="render exploded"
      />,
    );

    expect(html).toContain("Workspace chat could not render");
    expect(html).toContain("render exploded");
    expect(html).toContain('href="/workspace"');
    expect(html).toContain('href="/workspace/activity?thread=thread%20with%20spaces"');
    expect(html).toContain('href="/workspace/settings?thread=thread%20with%20spaces"');
  });

  it("opens activity inline without linking away from the current task", () => {
    const html = renderWorkspaceMarkup(<WorkspacePage />, "/workspace/activity");

    expect(html).not.toContain("Workspace activity posture");
    expect(html).not.toContain("Next best action");
    expect(html).not.toContain('href="/activity"');
    expect(html).toContain('aria-label="Task inspector"');
    expect(html).toContain("Activity");
    expect(html).toContain("Loading activity");
    expect(html).toContain("context stays open");
  });
});
