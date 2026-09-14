import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { QuestionCard, ScopeCard } from "./ChatApprovalCards";

describe("ChatApprovalCards", () => {
  it("renders a pending question with an amber reply affordance", () => {
    const html = renderToStaticMarkup(
      <QuestionCard
        ev={{
          kind: "question",
          question_id: "question_1234567890",
          grant_id: "grant_1",
          prompt: "Which branch should the reviewer inspect?",
          status: "pending",
        }}
      />,
    );

    expect(html).toContain("Agent asks");
    expect(html).toContain("Which branch should the reviewer inspect?");
    expect(html).toContain("reply...");
    expect(html).toContain("bg-signal-authority");
  });

  it("renders pending scope expansions with new read and write access", () => {
    const html = renderToStaticMarkup(
      <ScopeCard
        ev={{
          kind: "scope",
          request_id: "scope_1234567890",
          grant_id: "grant_1",
          reason: "Need to inspect generated artifacts.",
          requested: {
            read_patterns: ["outputs/**"],
            write_prefix: null,
            write_prefixes: ["reports/"],
            mode: "read_write",
            ttl_seconds: 60,
          },
          original: {
            allow_patterns: ["src/**"],
            outputs_prefix: null,
            write_prefixes: [],
            mode: "read",
          },
          approval_id: "approval_1",
          status: "awaiting_user",
        }}
      />,
    );

    expect(html).toContain("Subagent asks for more access");
    expect(html).toContain("+ read");
    expect(html).toContain("outputs/**");
    expect(html).toContain("+ write");
    expect(html).toContain("reports/");
    expect(html).toContain("Auto-denies after 60s.");
  });
});
