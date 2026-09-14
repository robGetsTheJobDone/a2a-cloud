import { describe, expect, it } from "vitest";
import {
  agentDetailRouteReference,
  agentDetailRouteStatus,
  agentRouteRecoveryMatches,
  agentSearchWithRequestedAgent,
  agentSearchWithoutRequestedAgent,
  requestedAgentNameFromSearch,
} from "./myAgentRouteState";

describe("my agent detail route state", () => {
  it("keeps a readable deep-link route reference visible", () => {
    expect(
      agentDetailRouteReference("app-flow-smoke-20260625t173231449-529287"),
    ).toBe("my agents/app flow smoke 20260625t173231449 529287");
  });

  it("classifies loading, missing, and failed lookups", () => {
    expect(agentDetailRouteStatus({ loading: true, error: null })).toBe("loading");
    expect(agentDetailRouteStatus({ loading: false, error: "404: Not found" })).toBe("missing");
    expect(agentDetailRouteStatus({ loading: false, error: "500: upstream" })).toBe("error");
    expect(agentDetailRouteStatus({ loading: false, error: null })).toBe("preparing");
  });

  it("carries a missing deep-link agent into the import route search", () => {
    const search = agentSearchWithRequestedAgent(
      "?section=fleet",
      "app-flow-smoke-20260625t173231449-529287",
    );

    expect(search).toBe(
      "?section=fleet&requested_agent=app-flow-smoke-20260625t173231449-529287",
    );
    expect(requestedAgentNameFromSearch(search)).toBe(
      "app-flow-smoke-20260625t173231449-529287",
    );
  });

  it("clears stale requested-agent context from regular fleet routes", () => {
    expect(
      agentSearchWithoutRequestedAgent(
        "?section=fleet&requested_agent=stale-agent",
      ),
    ).toBe("?section=fleet");
    expect(requestedAgentNameFromSearch("?requested_agent=+")).toBeNull();
  });

  it("ranks nearby owned-agent matches for a missing deep link", () => {
    const matches = agentRouteRecoveryMatches(
      "app-flow-smoke-20260625t173231449-529287",
      [
        { name: "billing-collector" },
        { name: "app-flow-smoke-20260625t173231449-529999" },
        { name: "app-flow-smoke-manual-check" },
        { name: "workspace-reviewer" },
      ],
    );

    expect(matches.map((agent) => agent.name)).toEqual([
      "app-flow-smoke-20260625t173231449-529999",
      "app-flow-smoke-manual-check",
    ]);
  });

  it("omits unrelated agents from missing-route recovery", () => {
    expect(
      agentRouteRecoveryMatches("missing-alpha", [
        { name: "billing-collector" },
        { name: "workspace-reviewer" },
      ]),
    ).toEqual([]);
  });

  it("does not suggest matches from generic agent tokens alone", () => {
    expect(
      agentRouteRecoveryMatches("missing-agent", [
        { name: "billing-agent" },
        { name: "review-agent" },
      ]),
    ).toEqual([]);
  });
});
