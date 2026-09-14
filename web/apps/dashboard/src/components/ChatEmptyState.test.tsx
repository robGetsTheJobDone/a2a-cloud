import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { ChatEmptyState } from "./ChatEmptyState";

describe("ChatEmptyState", () => {
  it("shows the available workspace file count in prompt suggestions", () => {
    const html = renderToStaticMarkup(
      <ChatEmptyState
        modelLabel="gpt-5 · default key"
        workspaceFileCount={3}
        onPickPrompt={() => undefined}
      />,
    );

    expect(html).toContain("Ask about workspace files");
    expect(html).toContain("3 workspace files");
  });

  it("marks capped workspace file counts as approximate", () => {
    const html = renderToStaticMarkup(
      <ChatEmptyState
        modelLabel="gpt-5 · default key"
        workspaceFileCount={1000}
        workspaceFileCountHasMore
        onPickPrompt={() => undefined}
      />,
    );

    expect(html).toContain("Ask about workspace files");
    expect(html).toContain("1000+ workspace files");
  });

  it("can show file prompts from presence without rendering a capped count", () => {
    const html = renderToStaticMarkup(
      <ChatEmptyState
        modelLabel="gpt-5 · default key"
        workspaceHasFiles
        workspaceFileCount={null}
        onPickPrompt={() => undefined}
      />,
    );

    expect(html).toContain("Ask about workspace files");
    expect(html).toContain("workspace files");
    expect(html).not.toContain("1000+ workspace files");
  });

  it("falls back to task prompts when no workspace files are available", () => {
    const html = renderToStaticMarkup(
      <ChatEmptyState
        modelLabel="gpt-5 · default key"
        workspaceFileCount={0}
        onPickPrompt={() => undefined}
      />,
    );

    expect(html).toContain("Start with a task");
    expect(html).toContain("no files in scope");
  });
});
