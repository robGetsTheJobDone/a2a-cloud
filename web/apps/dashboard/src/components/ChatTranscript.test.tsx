import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import {
  ChatMessageRow,
  ChatStreamingStatusRow,
  ChatThreadLoadingState,
  ChatToolChip,
} from "./ChatTranscript";

describe("ChatTranscript rows", () => {
  it("renders a user message as plain chat content", () => {
    const html = renderToStaticMarkup(
      <ChatMessageRow
        msg={{ role: "user", content: "Use the workspace report" }}
        streaming={false}
      />,
    );

    expect(html).toContain("Use the workspace report");
    expect(html).toContain("bg-ink");
  });

  it("renders assistant streaming state without draft content", () => {
    const html = renderToStaticMarkup(
      <ChatMessageRow msg={{ role: "assistant", content: "" }} streaming />,
    );

    expect(html).toContain("thinking");
    expect(html).toContain("bg-signal-authority");
  });

  it("renders the shared streaming status row", () => {
    const html = renderToStaticMarkup(
      <ChatStreamingStatusRow status="Running agent" draft="" />,
    );

    expect(html).toContain("Running agent");
    expect(html).toContain("bg-signal-authority");
  });

  it("renders the thread loading state", () => {
    const html = renderToStaticMarkup(<ChatThreadLoadingState />);

    expect(html).toContain("Loading chat thread");
    expect(html).toContain("loading thread");
    expect(html).toContain("Restoring transcript and agent activity.");
  });

  it("renders tool summaries through the shared tool chip", () => {
    const html = renderToStaticMarkup(
      <ChatToolChip
        ev={{ tool: "list_files", status: "ok", summary: "3 files" }}
      />,
    );

    expect(html).toContain("list_files");
    expect(html).toContain("3 files");
  });
});
