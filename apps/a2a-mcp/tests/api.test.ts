import { test, beforeEach, afterEach } from "node:test";
import assert from "node:assert/strict";

import { ApiError, ControlPlaneClient } from "../src/api.js";

const originalFetch = globalThis.fetch;

beforeEach(() => {
  globalThis.fetch = originalFetch;
});

afterEach(() => {
  globalThis.fetch = originalFetch;
});

test("ControlPlaneClient reports non-JSON error bodies without rereading them", async () => {
  globalThis.fetch = (async () =>
    new Response("<h1>upstream unavailable</h1>", {
      status: 502,
      statusText: "Bad Gateway",
      headers: { "content-type": "text/html" },
    })) as typeof fetch;

  await assert.rejects(
    () => new ControlPlaneClient("https://api.example.test", "token").me(),
    (err) => {
      assert.ok(err instanceof ApiError);
      assert.equal(err.status, 502);
      assert.match(err.message, /upstream unavailable/);
      assert.doesNotMatch(err.message, /Body is unusable/);
      return true;
    },
  );
});

test("ControlPlaneClient forwards bearer tokens and extracts JSON error detail", async () => {
  globalThis.fetch = (async (_input: RequestInfo | URL, init?: RequestInit) => {
    const headers = init?.headers as Record<string, string>;
    assert.equal(headers.authorization, "Bearer access-token");
    return new Response(JSON.stringify({ detail: "keycloak email is not verified" }), {
      status: 403,
      headers: { "content-type": "application/json" },
    });
  }) as typeof fetch;

  await assert.rejects(
    () => new ControlPlaneClient("https://api.example.test", "access-token").me(),
    /API 403: keycloak email is not verified/,
  );
});

test("ControlPlaneClient searches agents with query filters", async () => {
  let seenUrl = "";
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    seenUrl = String(input);
    const headers = init?.headers as Record<string, string>;
    assert.equal(headers.authorization, "Bearer access-token");
    return new Response(
      JSON.stringify([
        {
          name: "soc2-evidence",
          description: "SOC2 evidence agent",
          status: "running",
          public: true,
          url: "https://soc2.example.test",
          score: 0.9,
          match_source: "lexical",
          setup_required: false,
          skills: [{ name: "collect", description: "", tags: ["security"], input_fields: [] }],
        },
      ]),
      { status: 200, headers: { "content-type": "application/json" } },
    );
  }) as typeof fetch;

  const rows = await new ControlPlaneClient(
    "https://api.example.test",
    "access-token",
  ).searchAgents({
    query: "soc2",
    tags: ["security", "evidence"],
    skill: "collect",
    limit: 12,
  });

  const url = new URL(seenUrl);
  assert.equal(url.pathname, "/v1/agents/search");
  assert.equal(url.searchParams.get("q"), "soc2");
  assert.deepEqual(url.searchParams.getAll("tag"), ["security", "evidence"]);
  assert.equal(url.searchParams.get("skill"), "collect");
  assert.equal(url.searchParams.get("limit"), "12");
  assert.equal(rows[0].name, "soc2-evidence");
});
