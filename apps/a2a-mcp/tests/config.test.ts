import { test, beforeEach, afterEach } from "node:test";
import assert from "node:assert/strict";
import os from "node:os";
import path from "node:path";
import { promises as fs } from "node:fs";

import { addAgent, loadConfig, removeAgent } from "../src/config.js";
import { loadCredentials, saveCredentials } from "../src/credentials.js";

let originalHome: string | undefined;
let tmpHome: string;

beforeEach(async () => {
  originalHome = process.env.HOME;
  tmpHome = await fs.mkdtemp(path.join(os.tmpdir(), "a2a-mcp-"));
  process.env.HOME = tmpHome;
});

afterEach(async () => {
  if (originalHome !== undefined) process.env.HOME = originalHome;
  else delete process.env.HOME;
  await fs.rm(tmpHome, { recursive: true, force: true });
});

test("loadConfig returns empty when file is missing", async () => {
  const cfg = await loadConfig();
  assert.deepEqual(cfg, { version: 1, agents: [] });
});

test("addAgent persists and replaces by name", async () => {
  await addAgent({ name: "a", url: "https://x", addedAt: "t1" });
  await addAgent({ name: "b", url: "https://y", addedAt: "t1" });
  await addAgent({ name: "a", url: "https://z", addedAt: "t2" });
  const cfg = await loadConfig();
  assert.deepEqual(
    cfg.agents.map((a) => `${a.name}:${a.url}`),
    ["a:https://z", "b:https://y"],
  );
});

test("removeAgent reports hit/miss", async () => {
  await addAgent({ name: "a", url: "https://x", addedAt: "t" });
  assert.equal(await removeAgent("missing"), false);
  assert.equal(await removeAgent("a"), true);
  const cfg = await loadConfig();
  assert.equal(cfg.agents.length, 0);
});

test("credentials preserve Keycloak token metadata", async () => {
  await saveCredentials({
    apiUrl: "https://api.example.test",
    token: "access-token",
    refreshToken: "refresh-token",
    expiresAt: 12345,
    email: "dev@example.test",
    userId: 7,
    bucket: "user-7-files",
    issuer: "https://auth.example.test/realms/a2a",
    clientId: "a2acloud-cli",
    scope: "openid email mcp:invoke",
  });

  assert.deepEqual(await loadCredentials(), {
    apiUrl: "https://api.example.test",
    token: "access-token",
    refreshToken: "refresh-token",
    expiresAt: 12345,
    email: "dev@example.test",
    userId: 7,
    bucket: "user-7-files",
    issuer: "https://auth.example.test/realms/a2a",
    clientId: "a2acloud-cli",
    scope: "openid email mcp:invoke",
  });
});
