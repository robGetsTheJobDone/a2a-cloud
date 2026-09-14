import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import {
  ChatMessageContent,
  extractWorkspaceFileRefs as extractChatWorkspaceFileRefs,
} from "./ChatMessageContent";
import { WorkspaceInspectorProvider } from "../providers/WorkspaceInspectorContext";

describe("workspace file reference extraction", () => {
  it("does not turn email addresses into fake artifact cards", () => {
    const content = [
      "Latest message:",
      "",
      "From: Ada Example `ada@example.com`",
      "To: `robert@a2acloud.io`",
      "Mailu mailboxes found:",
      "",
      "`postmaster@a2acloud.io`",
      "`robert@a2acloud.io`",
    ].join("\n");

    expect(extractChatWorkspaceFileRefs(content)).toEqual([]);
  });

  it("still extracts explicit workspace refs and normal root filenames", () => {
    expect(
      extractChatWorkspaceFileRefs(
        "See `README.md`, `outputs/report.csv`, and workspace://inputs/raw.json",
      ).map((ref) => ref.path),
    ).toEqual(["README.md", "outputs/report.csv", "inputs/raw.json"]);
  });

  it("opens workspace references through the inline inspector when available", () => {
    const html = renderToStaticMarkup(
      <WorkspaceInspectorProvider>
        <ChatMessageContent
          content="Created `outputs/report.csv`"
          markdown={false}
        />
      </WorkspaceInspectorProvider>,
    );

    expect(html).toContain("outputs/report.csv");
    expect(html).toContain("Open preview for outputs/report.csv");
    expect(html).not.toContain('href="/workspace/artifacts/file/outputs/report.csv"');
  });
});
