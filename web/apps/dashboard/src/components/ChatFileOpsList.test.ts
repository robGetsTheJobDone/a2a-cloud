import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { StaticRouter } from "react-router-dom/server";
import { describe, expect, it } from "vitest";
import { WorkspaceInspectorProvider } from "../providers/WorkspaceInspectorContext";
import { ChatFileOpsList, summarizeFileOps } from "./ChatFileOpsList";

function renderFileOps(defaultOpen = false, withInspector = false) {
  const fileOps = createElement(ChatFileOpsList, {
    defaultOpen,
    ops: [
      {
        op: "create",
        path: "outputs/report.md",
        size: 128,
        content_type: "text/markdown",
      },
      {
        op: "update",
        path: "notes.txt",
        size: 64,
        content_type: "text/plain",
      },
      {
        op: "delete",
        path: "old-result.json",
        size: 32,
        content_type: "application/json",
      },
    ],
  });

  return renderToStaticMarkup(
    createElement(
      StaticRouter,
      { location: "/workspace" },
      withInspector
        ? createElement(WorkspaceInspectorProvider, null, fileOps)
        : fileOps,
    ),
  );
}

describe("chat file-op list helpers", () => {
  it("summarizes known operations in stable dashboard order", () => {
    expect(
      summarizeFileOps([
        { op: "update" },
        { op: "create" },
        { op: "update" },
        { op: "delete" },
      ]),
    ).toBe("1 create, 2 updates, 1 delete");
  });

  it("includes unknown operations after known operations", () => {
    expect(
      summarizeFileOps([
        { op: "rename" },
        { op: "create" },
        { op: "chmod" },
        { op: "rename" },
      ]),
    ).toBe("1 create, 1 chmod, 2 renames");
  });

  it("handles an empty operation list", () => {
    expect(summarizeFileOps([])).toBe("0 changes");
  });

  it("shows changed artifacts in the compact file-op list", () => {
    const html = renderFileOps();

    expect(html).toContain("file changes");
    expect(html).toContain("1 create, 1 update, 1 delete");
    expect(html).toContain("outputs/report.md");
    expect(html).toContain("notes.txt");
    expect(html).not.toContain("old-result.json");
  });

  it("includes deleted artifacts when the file-op list is expanded", () => {
    const html = renderFileOps(true);

    expect(html).toContain("old-result.json");
  });

  it("uses inline artifact controls when a workspace inspector is available", () => {
    const routedHtml = renderFileOps();
    const inlineHtml = renderFileOps(false, true);

    expect(routedHtml).toContain('<a title="outputs/report.md"');
    expect(inlineHtml).not.toContain('<a title="outputs/report.md"');
    expect(inlineHtml).toContain('<button type="button" title="outputs/report.md"');
  });
});
