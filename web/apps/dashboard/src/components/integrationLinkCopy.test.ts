import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { integrationLinkUrlLabel, integrationLinkUrlSecrecyNote } from "../api";

const PANELS = [
  "./InstalledAgents.tsx",
  "./agents/auth/AgentAccessPanel.tsx",
] as const;

function panelSource(relative: string): string {
  return readFileSync(fileURLToPath(new URL(relative, import.meta.url)), "utf8");
}

describe("integrationLinkUrlSecrecyNote", () => {
  it("only promises the URLs are safe for the header form", () => {
    expect(integrationLinkUrlSecrecyNote(false)).toContain("hold no secret");
  });

  it("warns instead of reassuring once the token goes in the URL", () => {
    const note = integrationLinkUrlSecrecyNote(true);
    expect(note).not.toContain("hold no secret");
    expect(note).toContain("must be treated as secrets");
  });
});

describe("integrationLinkUrlLabel", () => {
  it("leaves header-form URLs unlabelled", () => {
    expect(integrationLinkUrlLabel("MCP URL", { token_placement: "header" })).toBe(
      "MCP URL",
    );
  });

  it("marks URL-token URLs as secrets", () => {
    expect(
      integrationLinkUrlLabel("MCP URL", { token_placement: "url_query" }),
    ).toBe("MCP URL (secret)");
  });
});

describe("integration link panels", () => {
  // The regression: both panels used to state "the URLs below hold no secret"
  // as static copy, then render `?integration_token=<live token>` URLs right
  // underneath it when the URL-token box was ticked. Routing the sentence
  // through the helper makes the claim a function of that checkbox.
  it.each(PANELS)("derives the secrecy claim from the checkbox in %s", (panel) => {
    const source = panelSource(panel);
    expect(source).toContain("integrationLinkUrlSecrecyNote(urlToken)");
    expect(source).not.toContain("hold no secret");
  });

  it.each(PANELS)("labels the created link's URLs by placement in %s", (panel) => {
    const source = panelSource(panel);
    for (const label of ["OpenAPI URL", "MCP URL", "Sample invoke URL"]) {
      expect(source).toContain(`integrationLinkUrlLabel("${label}", newLink)`);
    }
    // ... and never as a bare literal, which would drop the "(secret)" marker.
    expect(source).not.toContain('label="MCP URL"');
    expect(source).not.toContain('label="OpenAPI URL"');
  });
});
