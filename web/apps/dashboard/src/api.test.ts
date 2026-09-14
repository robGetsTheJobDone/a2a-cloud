import { afterEach, describe, expect, it, vi } from "vitest";
import {
  browserSession,
  getWorkspaceFilePresence,
  getTrialRoom,
  getKernelEvolutionRun,
  getControlRoom,
  getUserKernelSimulationRun,
  hasWorkspaceFiles,
  listBounties,
  listBountiesPage,
  listAgentProofs,
  listAllFilesPaged,
  listActivity,
  listFilesPage,
  listOrganizations,
  listPublicAgentProofs,
  listPublicAgents,
} from "./api";

function file(path: string) {
  return {
    path,
    size: 1,
    modified_at: "2026-06-01T00:00:00+00:00",
    content_type: "text/plain",
  };
}

function jsonResponse(body: unknown, headers: Record<string, string> = {}) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: {
      "content-type": "application/json",
      ...headers,
    },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("browser session api", () => {
  it("accepts the compact authenticated session payload", async () => {
    const fetchMock = vi.fn(async () =>
      jsonResponse({
        authenticated: true,
        user: { id: 7, email: "dev@example.com" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(browserSession()).resolves.toEqual({
      id: 7,
      email: "dev@example.com",
      source: "cookie",
    });
    expect(fetchMock).toHaveBeenCalledWith("/v1/auth/session", { headers: {} });
  });
});

describe("file listing api", () => {
  it("requests bounded file pages and exposes the next cursor", async () => {
    const fetchMock = vi.fn(async () =>
      jsonResponse([file("data/input.csv")], {
        "x-a2a-next-cursor": "next-token",
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const page = await listFilesPage(undefined, {
      limit: 1,
      cursor: "start-token",
    });

    expect(fetchMock).toHaveBeenCalledWith("/v1/me/files?limit=1&cursor=start-token", {
      headers: {},
    });
    expect(page).toEqual({
      items: [file("data/input.csv")],
      nextCursor: "next-token",
    });
  });

  it("follows cursors for complete recursive file lists", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        jsonResponse([file("a.txt"), file("b.txt")], {
          "x-a2a-next-cursor": "page-2",
        }),
      )
      .mockResolvedValueOnce(jsonResponse([file("c.txt")]));
    vi.stubGlobal("fetch", fetchMock);

    const files = await listAllFilesPaged(2);

    expect(fetchMock).toHaveBeenNthCalledWith(1, "/v1/me/files?limit=2", {
      headers: {},
    });
    expect(fetchMock).toHaveBeenNthCalledWith(2, "/v1/me/files?limit=2&cursor=page-2", {
      headers: {},
    });
    expect(files.map((item) => item.path)).toEqual(["a.txt", "b.txt", "c.txt"]);
  });

  it("checks workspace file presence with a single-item page", async () => {
    const fetchMock = vi.fn(async () =>
      jsonResponse({ has_files: true, has_more: true }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(hasWorkspaceFiles()).resolves.toBe(true);
    expect(fetchMock).toHaveBeenCalledWith("/v1/me/files?limit=1&summary=true", {
      headers: {},
    });
  });

  it("loads bounded workspace file presence with count state", async () => {
    const fetchMock = vi.fn(async () =>
      jsonResponse({
        has_files: true,
        file_count: 100,
        has_more: true,
        next_cursor: "page-2",
        limit: 100,
        status: "truncated",
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await expect(getWorkspaceFilePresence()).resolves.toEqual({
      has_files: true,
      file_count: 100,
      has_more: true,
      next_cursor: "page-2",
      limit: 100,
      status: "truncated",
    });
    expect(fetchMock).toHaveBeenCalledWith("/v1/me/files?limit=100&summary=true", {
      headers: {},
    });
  });
});

describe("agent proof api", () => {
  it("requests compact private proof lists when requested", async () => {
    const fetchMock = vi.fn(async () => jsonResponse([]));
    vi.stubGlobal("fetch", fetchMock);

    await listAgentProofs({ limit: 25, compact: true });

    expect(fetchMock).toHaveBeenCalledWith(
      "/v1/me/agent-proofs?limit=25&compact=true",
      { headers: {} },
    );
  });

  it("requests compact public proof summaries by default", async () => {
    const fetchMock = vi.fn(async () => jsonResponse([]));
    vi.stubGlobal("fetch", fetchMock);

    await listPublicAgentProofs();

    expect(fetchMock).toHaveBeenCalledWith("/v1/public/agent-proofs?compact=true");
  });
});

describe("public agent api", () => {
  it("passes public agent pagination parameters", async () => {
    const fetchMock = vi.fn(async () => jsonResponse([]));
    vi.stubGlobal("fetch", fetchMock);

    await listPublicAgents({ limit: 50, cursor: 12 });

    expect(fetchMock).toHaveBeenCalledWith("/v1/public/agents?limit=50&cursor=12");
  });
});

describe("organization api", () => {
  it("requests the bounded chat organization preference", async () => {
    const fetchMock = vi.fn(async () => jsonResponse([]));
    vi.stubGlobal("fetch", fetchMock);

    await listOrganizations({ purpose: "chat", limit: 1 });

    expect(fetchMock).toHaveBeenCalledWith("/v1/me/organizations?purpose=chat&limit=1", {
      headers: {},
    });
  });
});

describe("activity api", () => {
  it("forwards deep-link filters to the work ledger endpoint", async () => {
    const fetchMock = vi.fn(async () => jsonResponse({ items: [], next_cursor: null }));
    vi.stubGlobal("fetch", fetchMock);

    await listActivity({
      agent: "billing-agent",
      source: "proof",
      grant: "grant/with space",
      status: "failed",
      limit: 25,
    });

    expect(fetchMock).toHaveBeenCalledWith(
      "/v1/me/activity?limit=25&status=failed&source=proof&agent=billing-agent&grant=grant%2Fwith+space",
      { headers: {} },
    );
  });
});

describe("control room api", () => {
  it("passes the requested timeline limit to the control-room endpoint", async () => {
    const fetchMock = vi.fn(async () =>
      jsonResponse({
        policy: {
          monthly_budget_cents: 5000,
          run_budget_cents: 500,
          max_agent_calls_per_run: 8,
          require_approval_for_file_writes: false,
          deny_external_network: false,
          only_approved_agents: false,
          pii_safe_mode: false,
          approved_agents: [],
        },
        summary: {
          monthly_spend_cents: 0,
          monthly_budget_cents: 5000,
          run_budget_cents: 500,
          llm_calls_month: 0,
          llm_tokens_month: 0,
          agent_runs: 0,
          dag_runs: 0,
          failures: 0,
          files_touched: 0,
        },
        timeline: [],
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await getControlRoom({ limit: 40 });

    expect(fetchMock).toHaveBeenCalledWith("/v1/me/control-room?limit=40", {
      headers: {},
    });
  });
});

describe("trial room api", () => {
  it("fetches one owner-scoped trial room by slug", async () => {
    const fetchMock = vi.fn(async () => jsonResponse({ slug: "older/room" }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(getTrialRoom("older/room")).resolves.toEqual({ slug: "older/room" });

    expect(fetchMock).toHaveBeenCalledWith("/v1/me/trial-rooms/older%2Froom", {
      headers: {},
    });
  });
});

describe("kernel simulation api", () => {
  it("fetches owner-scoped simulation and evolution runs by job id", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({ job: { job_id: "sim/run 1" }, events: [] }))
      .mockResolvedValueOnce(jsonResponse({ job: { job_id: "evo/run 2" }, events: [] }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(getUserKernelSimulationRun("sim/run 1")).resolves.toEqual({
      job: { job_id: "sim/run 1" },
      events: [],
    });
    await expect(getKernelEvolutionRun("evo/run 2")).resolves.toEqual({
      job: { job_id: "evo/run 2" },
      events: [],
    });

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      "/v1/me/kernel-simulations/runs/sim%2Frun%201",
      { headers: {} },
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "/v1/me/kernel-evolution/runs/evo%2Frun%202",
      { headers: {} },
    );
  });
});

describe("bounty api", () => {
  it("requests bounded bounty pages and exposes the next offset", async () => {
    const fetchMock = vi.fn(async () =>
      jsonResponse([], { "x-a2a-next-offset": "150" }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const page = await listBountiesPage({
      mine: true,
      status: "open",
      limit: 50,
      offset: 100,
    });

    expect(fetchMock).toHaveBeenCalledWith(
      "/v1/bounties?mine=true&status=open&limit=50&offset=100",
      { headers: {} },
    );
    expect(page).toEqual({ items: [], nextOffset: 150 });
  });

  it("follows bounty offsets for the backwards-compatible full list", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        jsonResponse([{ slug: "first" }], { "x-a2a-next-offset": "1" }),
      )
      .mockResolvedValueOnce(jsonResponse([{ slug: "second" }]));
    vi.stubGlobal("fetch", fetchMock);

    const bounties = await listBounties();

    expect(fetchMock).toHaveBeenNthCalledWith(1, "/v1/bounties?limit=100", {
      headers: {},
    });
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "/v1/bounties?limit=100&offset=1",
      { headers: {} },
    );
    expect(bounties.map((bounty) => bounty.slug)).toEqual(["first", "second"]);
  });
});
