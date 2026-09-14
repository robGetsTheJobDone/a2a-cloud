import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import {
  WorkspaceInspectorProvider,
  useOptionalWorkspaceInspector,
  useWorkspaceInspector,
} from "./WorkspaceInspectorContext";

function InspectorProbe() {
  const inspector = useWorkspaceInspector();
  return createElement(
    "span",
    {
      "data-panel": inspector.panel ?? "closed",
      "data-artifact": inspector.selectedArtifact?.path ?? "none",
    },
    "inspector",
  );
}

function OptionalInspectorProbe() {
  const inspector = useOptionalWorkspaceInspector();
  return createElement("span", null, inspector ? "available" : "unavailable");
}

describe("WorkspaceInspectorProvider", () => {
  it("starts closed by default", () => {
    const html = renderToStaticMarkup(
      createElement(
        WorkspaceInspectorProvider,
        null,
        createElement(InspectorProbe),
      ),
    );

    expect(html).toContain('data-panel="closed"');
    expect(html).toContain('data-artifact="none"');
  });

  it("accepts an initial panel", () => {
    const html = renderToStaticMarkup(
      createElement(
        WorkspaceInspectorProvider,
        {
          initialPanel: "activity",
          children: createElement(InspectorProbe),
        },
      ),
    );

    expect(html).toContain('data-panel="activity"');
  });

  it("offers an optional hook for consumers with a routed fallback", () => {
    const html = renderToStaticMarkup(createElement(OptionalInspectorProbe));

    expect(html).toContain("unavailable");
  });

  it("throws a focused error when the strict hook has no provider", () => {
    expect(() => renderToStaticMarkup(createElement(InspectorProbe))).toThrow(
      "useWorkspaceInspector must be used within a WorkspaceInspectorProvider",
    );
  });
});
