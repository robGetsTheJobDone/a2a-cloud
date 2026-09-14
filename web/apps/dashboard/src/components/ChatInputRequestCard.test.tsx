import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { JsonSchema } from "../api";
import { ChatInputRequestCard } from "./ChatInputRequestCard";

const requestSchema: JsonSchema = {
  type: "object",
  required: ["evidence"],
  properties: {
    evidence: {
      type: "object",
      title: "Evidence file",
      format: "a2a-file-ref",
      description: "Upload the audit artifact.",
    },
    notes: {
      type: "string",
      title: "Notes",
      description: "Context for the reviewer.",
    },
  },
};

describe("ChatInputRequestCard", () => {
  it("renders pending structured input with field and file metadata", () => {
    const html = renderToStaticMarkup(
      <ChatInputRequestCard
        ev={{
          kind: "input",
          request_id: "req_1234567890",
          grant_id: "grant_1",
          title: "Upload evidence",
          reason: "The reviewer needs the source artifact.",
          schema: requestSchema,
          ui_schema: {},
          status: "pending",
        }}
      />,
    );

    expect(html).toContain("Agent needs structured input");
    expect(html).toContain("2 fields / 1 required / 1 file field");
    expect(html).toContain("Upload evidence");
    expect(html).toContain("Choose file");
    expect(html).toContain("The tool resumes after submission.");
  });

  it("renders submitted previews without the input form", () => {
    const html = renderToStaticMarkup(
      <ChatInputRequestCard
        ev={{
          kind: "input",
          request_id: "req_submitted",
          grant_id: "grant_1",
          title: "Runtime parameters",
          reason: "",
          schema: requestSchema,
          ui_schema: {},
          status: "submitted",
          value_preview: { notes: "approved" },
        }}
      />,
    );

    expect(html).toContain("Input submitted");
    expect(html).toContain("&quot;notes&quot;: &quot;approved&quot;");
    expect(html).not.toContain("The tool resumes after submission.");
  });
});
