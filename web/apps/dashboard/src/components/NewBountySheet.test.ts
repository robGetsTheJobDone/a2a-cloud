import { describe, expect, it } from "vitest";

import { bountyShareUrl } from "./NewBountySheet";

describe("bountyShareUrl", () => {
  it("builds a public, channel-attributed recruiting link", () => {
    const url = new URL(bountyShareUrl("summarize-inbox", "copy"));

    expect(url.origin).toBe(globalThis.location?.origin || "http://localhost");
    expect(url.pathname).toBe("/bounties/summarize-inbox");
    expect(url.searchParams.get("utm_source")).toBe("bounty_poster");
    expect(url.searchParams.get("utm_medium")).toBe("copy");
    expect(url.searchParams.get("utm_campaign")).toBe("bounty_recruitment");
    expect(url.searchParams.get("utm_id")).toBe("bounty:summarize-inbox");
  });

  it("encodes unusual slugs without changing attribution content", () => {
    const url = new URL(bountyShareUrl("agent review/one", "native share"));

    expect(url.pathname).toBe("/bounties/agent%20review%2Fone");
    expect(url.searchParams.get("utm_medium")).toBe("native share");
    expect(url.searchParams.get("utm_content")).toBe("agent review/one");
  });
});
