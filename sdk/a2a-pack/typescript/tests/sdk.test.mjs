import assert from "node:assert/strict";
import { generateKeyPairSync } from "node:crypto";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { after, test } from "node:test";

import {
  A2AAgent,
  HttpA2AClient,
  InMemoryA2AClient,
  EventRecorder,
  GrantDelegationDenied,
  LocalRunContext,
  LocalWorkspaceClient,
  ReplayDivergence,
  ReplayLLM,
  ReplayToolCaller,
  ReplayWorkspaceClient,
  apiKeyAuth,
  CancelledByCaller,
  compileAgent,
  ConsumerSetupMissing,
  ConsumerSetup,
  ConsumerSetupField,
  customAuth,
  jwtAuth,
  MissingScopes,
  platformUserAuth,
  publicAuth,
  replaySession,
  RunContext,
  SandboxUnavailable,
  filterEvents,
  hashInput,
  mintGrant,
  serveAgent,
  SkillInputError,
  verifyGrant,
  verifyReceipt,
  verifyReplaySession,
  skill,
  writeAgentDsl
} from "../dist/index.js";

class MathAgent extends A2AAgent {
  static agent = {
    name: "math-agent",
    description: "Typed math fixture",
    version: "0.1.0"
  };
  static auth = publicAuth();
  static requiredEnv = ["REGION"];
  static requiredSecrets = ["API_TOKEN"];
  static consumerSetup = ConsumerSetup.fromFields(
    ConsumerSetupField.secret("GITHUB_TOKEN", {
      label: "GitHub token",
      description: "Caller token used to read repositories."
    }),
    ConsumerSetupField.config("PLAN", {
      label: "Plan",
      input_type: "select",
      options: ["free", "pro"]
    }),
    ConsumerSetupField.config("ENABLE_REVIEW", {
      label: "Enable review",
      required: false,
      input_type: "boolean"
    })
  );
  static runtime = {
    llm_provisioning: "platform",
    tools_used: ["typescript"],
    resources: { cpu: "500m", memory: "512Mi" }
  };
  static skills = [
    skill({
      name: "sum",
      handler: "sumHandler",
      description: "Add two integers",
      input_schema: {
        type: "object",
        properties: {
          left: { type: "integer" },
          right: { type: "integer" }
        },
        required: ["left", "right"],
        additionalProperties: false
      },
      output_schema: { type: "integer" },
      policy: { timeout_seconds: 10, idempotent: true }
    }),
    skill({
      name: "context_sum",
      handler: "contextSumHandler",
      description: "Add two integers using RunContext",
      input_schema: {
        type: "object",
        properties: {
          left: { type: "integer" },
          right: { type: "integer" }
        },
        required: ["left", "right"],
        additionalProperties: false
      },
      output_schema: {
        type: "object",
        properties: {
          total: { type: "integer" },
          region: { type: "string" },
          token: { type: "string" },
          model: { type: "string" },
          cp_url: { type: ["string", "null"] }
        },
        required: ["total", "region", "token", "model", "cp_url"],
        additionalProperties: false
      }
    })
  ];

  async sumHandler(request) {
    await request.emit?.({ type: "progress", step: "sum" });
    return {
      result: request.arguments.left + request.arguments.right,
      events: [{ kind: "tested" }]
    };
  }

  async contextSumHandler(ctx, input) {
    assert.ok(ctx instanceof RunContext);
    ctx.requireScopes(["math:add"]);
    await ctx.emitProgress("context ready", { left: input.left });
    await ctx.emitArtifact({
      path: "memory://context-sum.json",
      content_type: "application/json"
    });
    return {
      total: input.left + input.right,
      region: ctx.consumerConfig("REGION", "unknown"),
      token: ctx.consumerSecret("TOKEN"),
      model: ctx.llm.model,
      cp_url: ctx.cpUrl()
    };
  }
}

class ConfigLifecycleAgent extends A2AAgent {
  static agent = {
    name: "config-lifecycle-agent",
    description: "Config and lifecycle fixture"
  };
  static configSchema = {
    type: "object",
    properties: {
      prefix: { type: "string", default: "hello" },
      retries: { type: "integer", default: 2 }
    },
    required: ["prefix"],
    additionalProperties: false
  };
  static skills = [
    skill({
      name: "greet",
      handler: "greet",
      input_schema: {
        type: "object",
        properties: {
          who: { type: "string" }
        },
        required: ["who"],
        additionalProperties: false
      },
      output_schema: {
        type: "object",
        properties: {
          message: { type: "string" },
          retries: { type: "integer" }
        },
        required: ["message", "retries"],
        additionalProperties: false
      }
    })
  ];
  startupCount = 0;
  shutdownCount = 0;

  async startup() {
    this.startupCount += 1;
  }

  async shutdown() {
    this.shutdownCount += 1;
  }

  async health() {
    return this.startupCount === 1;
  }

  async greet(ctx, input) {
    await ctx.emitProgress("greeting");
    return {
      message: `${this.config.prefix} ${input.who}`,
      retries: this.config.retries
    };
  }
}

class ReplayAgent extends A2AAgent {
  static agent = {
    name: "replay-agent",
    description: "Replay fixture"
  };
  static skills = [
    skill({
      name: "draws",
      handler: "draws",
      input_schema: {
        type: "object",
        properties: {
          n: { type: "integer", default: 4 }
        },
        required: [],
        additionalProperties: false
      },
      output_schema: {
        type: "object",
        properties: {
          values: {
            type: "array",
            items: { type: "integer" }
          }
        },
        required: ["values"],
        additionalProperties: false
      }
    })
  ];

  async draws(ctx, input) {
    const n = input.n ?? 4;
    return {
      values: Array.from({ length: n }, () => Math.floor(ctx.random() * 1000))
    };
  }
}

class ReplayMutantAgent extends ReplayAgent {
  async draws(ctx, input) {
    ctx.workspace.write("not/in/recording.txt", "hello");
    return super.draws(ctx, input);
  }
}

test("compileAgent emits sidecar Agent DSL", () => {
  const dsl = compileAgent(MathAgent, {
    entrypoint: { command: ["node", "dist/worker.js"] },
    metadata: { test: true }
  });

  assert.equal(dsl.schema_version, "2026-06-04");
  assert.equal(dsl.language, "typescript");
  assert.equal(dsl.name, "math-agent");
  assert.deepEqual(dsl.entrypoint.command, ["node", "dist/worker.js"]);
  assert.deepEqual(dsl.required_env, ["REGION"]);
  assert.deepEqual(dsl.required_secrets, ["API_TOKEN"]);
  assert.deepEqual(dsl.consumer_setup.fields.map((field) => field.name), [
    "GITHUB_TOKEN",
    "PLAN",
    "ENABLE_REVIEW"
  ]);
  const setupFields = Object.fromEntries(dsl.consumer_setup.fields.map((field) => [field.name, field]));
  assert.equal(setupFields.GITHUB_TOKEN.kind, "secret");
  assert.equal(setupFields.GITHUB_TOKEN.input_type, "password");
  assert.equal(setupFields.GITHUB_TOKEN.description, "Caller token used to read repositories.");
  assert.equal(setupFields.PLAN.kind, "config");
  assert.equal(setupFields.PLAN.input_type, "select");
  assert.deepEqual(setupFields.PLAN.options, ["free", "pro"]);
  assert.equal(setupFields.ENABLE_REVIEW.input_type, "boolean");
  assert.equal(setupFields.ENABLE_REVIEW.required, false);
  assert.equal(dsl.auth.strategy, "public");
  assert.equal(dsl.skills[0].handler, "sumHandler");
  assert.deepEqual(dsl.skills[0].input_schema.required, ["left", "right"]);
  assert.equal(dsl.skills[0].policy.timeout_seconds, 10);
  assert.equal(dsl.skills[0].policy.idempotent, true);
  assert.equal(dsl.runtime.sandbox, "microsandbox");
  assert.deepEqual(dsl.metadata, { test: true });
});

test("ConsumerSetup validates field names and duplicate declarations", () => {
  assert.throws(() => ConsumerSetupField.config("bad-name"), /environment variable syntax/);
  assert.throws(
    () => ConsumerSetup.fromFields(
      ConsumerSetupField.config("PLAN"),
      ConsumerSetupField.secret("PLAN")
    ),
    /duplicate consumer setup field/
  );
  assert.deepEqual(
    ConsumerSetup.from_fields(ConsumerSetupField.config("PLAN")).required_names,
    ["PLAN"]
  );
});

test("auth helpers emit Python-compatible principal contracts", () => {
  const api = apiKeyAuth();
  assert.equal(api.model, "a2a_pack.auth.APIKeyAuth");
  assert.equal(api.strategy, "api_key");
  assert.equal(api.required, true);
  assert.deepEqual(api.principal_schema.required, ["api_key_id"]);
  assert.equal(api.principal_schema.properties.api_key_id.type, "string");

  const platform = platformUserAuth();
  assert.equal(platform.model, "a2a_pack.auth.PlatformUserAuth");
  assert.equal(platform.strategy, "platform_user");
  assert.deepEqual(platform.principal_schema.required, ["sub"]);
  assert.equal(platform.principal_schema.properties.user_id.anyOf[0].type, "integer");

  const jwt = jwtAuth({ resolver: "a2a_pack.auth.StaticAuthResolver" });
  assert.equal(jwt.model, "a2a_pack.auth.JWTAuth");
  assert.equal(jwt.strategy, "jwt");
  assert.equal(jwt.resolver, "a2a_pack.auth.StaticAuthResolver");
  assert.deepEqual(jwt.principal_schema.required, ["sub"]);

  const custom = customAuth({
    model: "agents.auth.CustomPrincipal",
    principal_schema: { type: "object", properties: { tenant: { type: "string" } }, required: ["tenant"] }
  });
  assert.equal(custom.strategy, "custom");
  assert.equal(custom.model, "agents.auth.CustomPrincipal");
});

test("writeAgentDsl writes JSON contract", async () => {
  const dir = await mkdtemp(join(tmpdir(), "a2a-ts-"));
  const path = join(dir, ".a2a", "agent.dsl.json");
  try {
    const dsl = await writeAgentDsl(MathAgent, path);
    const saved = JSON.parse(await readFile(path, "utf8"));
    assert.equal(saved.name, dsl.name);
    assert.equal(saved.skills[0].input_schema.properties.left.type, "integer");
  } finally {
    await rm(dir, { recursive: true, force: true });
  }
});

test("serveAgent dispatches to implemented agent method", async () => {
  const server = serveAgent(MathAgent, { port: 0 });
  after(() => server.close());

  await new Promise((resolve) => server.once("listening", resolve));
  const address = server.address();
  assert.equal(typeof address, "object");
  const url = `http://127.0.0.1:${address.port}/_a2a/invoke/sumHandler`;
  const response = await fetch(url, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      agent: "math-agent",
      skill: "sum",
      handler: "sumHandler",
      arguments: { left: 4, right: 5 }
    })
  });
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), {
    result: 9,
    events: [{ kind: "tested" }]
  });
});

test("serveAgent dispatches RunContext handlers and wraps raw results", async () => {
  const server = serveAgent(MathAgent, { port: 0 });
  after(() => server.close());

  await new Promise((resolve) => server.once("listening", resolve));
  const address = server.address();
  assert.equal(typeof address, "object");
  const url = `http://127.0.0.1:${address.port}/_a2a/invoke/contextSumHandler`;
  const response = await fetch(url, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      agent: "math-agent",
      skill: "context_sum",
      handler: "contextSumHandler",
      arguments: { left: 4, right: 5 },
      auth: { scopes: ["math:add"] },
      consumer_config: { REGION: "us-east" },
      consumer_secrets: { TOKEN: "secret-token" },
      llm_creds: {
        base_url: "http://litellm:4000/v1",
        api_key: "caller-key",
        model: "gpt-test"
      },
      cp_url: "https://api.example.test"
    })
  });

  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), {
    result: {
      total: 9,
      region: "us-east",
      token: "secret-token",
      model: "gpt-test",
      cp_url: "https://api.example.test"
    },
    events: [
      { kind: "progress", payload: { message: "context ready", left: 4 } },
      {
        kind: "artifact",
        payload: {
          path: "memory://context-sum.json",
          content_type: "application/json"
        }
      }
    ],
    artifacts: [
      {
        path: "memory://context-sum.json",
        content_type: "application/json"
      }
    ]
  });
});

test("A2AAgent validates config defaults and localInvoke mirrors Python harness", async () => {
  const agent = new ConfigLifecycleAgent();

  assert.deepEqual(agent.config, { prefix: "hello", retries: 2 });
  assert.deepEqual(await agent.localInvoke("greet", { who: "Ada" }), {
    message: "hello Ada",
    retries: 2
  });
  await assert.rejects(
    () => agent.localInvoke("greet", { who: "Ada", extra: true }),
    SkillInputError
  );
  assert.throws(() => new ConfigLifecycleAgent({ prefix: 42 }), /config.prefix/);
});

test("serveAgent runs lifecycle hooks and exposes worker health", async () => {
  const server = serveAgent(ConfigLifecycleAgent, { port: 0 });
  await new Promise((resolve) => server.once("listening", resolve));
  const address = server.address();
  assert.equal(typeof address, "object");
  const base = `http://127.0.0.1:${address.port}`;

  const health = await fetch(`${base}/_a2a/health`);
  assert.equal(health.status, 200);
  assert.deepEqual(await health.json(), { ok: true });

  const response = await fetch(`${base}/_a2a/invoke/greet`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      agent: "config-lifecycle-agent",
      skill: "greet",
      handler: "greet",
      arguments: { who: "Grace" }
    })
  });
  assert.equal(response.status, 200);
  assert.equal((await response.json()).result.message, "hello Grace");

  await new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
});

test("serveAgent rejects invalid input before invoking the handler", async () => {
  const server = serveAgent(ConfigLifecycleAgent, { port: 0 });
  after(() => server.close());
  await new Promise((resolve) => server.once("listening", resolve));
  const address = server.address();
  assert.equal(typeof address, "object");
  const response = await fetch(`http://127.0.0.1:${address.port}/_a2a/invoke/greet`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      agent: "config-lifecycle-agent",
      skill: "greet",
      handler: "greet",
      arguments: {}
    })
  });
  assert.equal(response.status, 500);
  assert.match((await response.json()).error, /input.who is required/);
});

test("receipts and replay sessions sign verify and emit from dispatch when configured", async () => {
  const previousReceiptSigning = process.env.A2A_RECEIPT_SIGNING_KEY;
  const previousReceiptVerify = process.env.A2A_RECEIPT_VERIFYING_KEY;
  const previousReplaySigning = process.env.A2A_REPLAY_SIGNING_KEY;
  const previousReplayVerify = process.env.A2A_REPLAY_VERIFYING_KEY;
  const receiptKeys = generateKeyPairSync("ed25519", {
    publicKeyEncoding: { type: "spki", format: "pem" },
    privateKeyEncoding: { type: "pkcs8", format: "pem" }
  });
  const replayKeys = generateKeyPairSync("ed25519", {
    publicKeyEncoding: { type: "spki", format: "pem" },
    privateKeyEncoding: { type: "pkcs8", format: "pem" }
  });
  try {
    process.env.A2A_RECEIPT_SIGNING_KEY = receiptKeys.privateKey;
    process.env.A2A_RECEIPT_VERIFYING_KEY = receiptKeys.publicKey;
    process.env.A2A_REPLAY_SIGNING_KEY = replayKeys.privateKey;
    process.env.A2A_REPLAY_VERIFYING_KEY = replayKeys.publicKey;

    assert.equal(hashInput({ b: 2, a: 1 }), hashInput({ a: 1, b: 2 }));
    const recorder = new EventRecorder({
      agent_name: "config-lifecycle-agent",
      skill_name: "greet",
      input_hash: hashInput({ who: "Ada" })
    });
    recorder.record("skill_start", { ok: true });
    assert.equal(filterEvents(recorder.buildSession(), "skill_start").length, 1);

    const response = await new ConfigLifecycleAgent().dispatch({
      agent: "config-lifecycle-agent",
      skill: "greet",
      handler: "greet",
      arguments: { who: "Ada" },
      caller: { sub: "user-2" },
      grant_ids: ["grant-1"],
      random_seed: "seed-1"
    });
    const receiptEvent = response.events.find((event) => event.kind === "receipt_sealed");
    const replayEvent = response.events.find((event) => event.kind === "replay_sealed");
    assert.ok(receiptEvent);
    assert.ok(replayEvent);
    const receipt = verifyReceipt(receiptEvent.payload.token);
    assert.equal(receipt.agent_name, "config-lifecycle-agent");
    assert.equal(receipt.skill_name, "greet");
    assert.equal(receipt.caller, "user-2");
    assert.deepEqual(receipt.grant_ids, ["grant-1"]);
    const replay = verifyReplaySession(replayEvent.payload.token);
    assert.equal(replay.skill_name, "greet");
    assert.equal(replay.random_seed, "seed-1");
    assert.deepEqual(replay.events.map((event) => event.kind), ["skill_start", "skill_end"]);
  } finally {
    restoreEnv("A2A_RECEIPT_SIGNING_KEY", previousReceiptSigning);
    restoreEnv("A2A_RECEIPT_VERIFYING_KEY", previousReceiptVerify);
    restoreEnv("A2A_REPLAY_SIGNING_KEY", previousReplaySigning);
    restoreEnv("A2A_REPLAY_VERIFYING_KEY", previousReplayVerify);
  }
});

test("LocalRunContext binds local clients secrets cancellation and artifacts", async () => {
  const workspace = {
    readText(path) {
      return `read:${path}`;
    },
    write(path, data) {
      this.lastWrite = { path, data };
    }
  };
  const ctx = new LocalRunContext({
    agent: "local",
    skill: "work",
    secrets: { TOKEN: "local-secret" },
    workspace
  });

  assert.equal(ctx.secret("TOKEN"), "local-secret");
  assert.equal(ctx.workspace.readText("a.txt"), "read:a.txt");
  ctx.workspace.write("out.txt", "ok");
  assert.deepEqual(workspace.lastWrite, { path: "out.txt", data: "ok" });
  const artifact = await ctx.writeArtifact("result.txt", "done", "text/plain");
  assert.equal(artifact.uri, "memory://local-task/result.txt");
  assert.equal(new TextDecoder().decode(ctx.artifactBytes.get("result.txt")), "done");
  ctx.cancel();
  await assert.rejects(() => ctx.checkCancelled(), CancelledByCaller);
});

test("LocalWorkspaceClient mirrors Python local workspace helpers", async () => {
  const previousSigning = process.env.A2A_GRANT_SIGNING_KEY;
  const previousVerify = process.env.A2A_GRANT_VERIFYING_KEY;
  const keys = generateKeyPairSync("ed25519", {
    publicKeyEncoding: { type: "spki", format: "pem" },
    privateKeyEncoding: { type: "pkcs8", format: "pem" }
  });
  try {
    process.env.A2A_GRANT_SIGNING_KEY = keys.privateKey;
    process.env.A2A_GRANT_VERIFYING_KEY = keys.publicKey;
    const workspace = new LocalWorkspaceClient({
      "docs/readme.md": "hello local workspace",
      "src/app.ts": "export const topic = 'parity';"
    }, {
      bucket: "local-bucket",
      allow_patterns: ["**"],
      outputs_prefix: "outputs/",
      write_prefixes: ["outputs/"]
    });

    assert.equal(workspace.exists("docs/readme.md"), true);
    assert.equal(workspace.readText("docs/readme.md"), "hello local workspace");
    workspace.writeText("outputs/result.txt", "done");
    assert.equal(workspace.readText("outputs/result.txt"), "done");
    assert.deepEqual(workspace.iterPaths(), ["docs/readme.md", "outputs/result.txt", "src/app.ts"]);
    assert.deepEqual(workspace.search("parity").map((item) => item.path), ["src/app.ts"]);
    assert.deepEqual(workspace.view("docs/readme.md", { maxBytes: 5 }), {
      path: "docs/readme.md",
      content: "hello",
      truncated: true,
      bytes: 21
    });
    assert.throws(() => workspace.writeText("private/result.txt", "nope"), /workspace write denied/);

    const child = verifyGrant(await workspace.delegate({
      audience: "child",
      allow_patterns: ["**"],
      outputs_prefix: "outputs/child/",
      write_prefixes: ["outputs/child/"]
    }));
    assert.equal(child.bucket, "local-bucket");
    assert.deepEqual(child.write_prefixes, ["outputs/child/"]);
  } finally {
    restoreEnv("A2A_GRANT_SIGNING_KEY", previousSigning);
    restoreEnv("A2A_GRANT_VERIFYING_KEY", previousVerify);
  }
});

test("replay doubles consume recorded side effects and detect divergence", () => {
  const recorder = new EventRecorder({ agent_name: "a", skill_name: "s" });
  recorder.record("workspace_read", { path: "data/a.txt", data: "alpha" });
  recorder.record("workspace_write", { path: "out/result.txt" });
  recorder.record("llm_call", { model: "m-1", prompt: "hello" });
  recorder.record("llm_response", { response: "world" });
  recorder.record("tool_call", { name: "search", q: "foo" });
  recorder.record("tool_response", { response: [1, 2, 3] });
  const session = recorder.buildSession();

  const workspace = new ReplayWorkspaceClient(session);
  assert.equal(new TextDecoder().decode(workspace.read("data/a.txt")), "alpha");
  workspace.write("out/result.txt", "ok");
  assert.throws(() => workspace.read("data/b.txt"), ReplayDivergence);

  const llm = new ReplayLLM(session);
  assert.equal(llm.complete({ model: "m-1", prompt: "hello" }), "world");
  assert.throws(() => new ReplayLLM(session).complete({ model: "m-2", prompt: "hello" }), ReplayDivergence);

  const tools = new ReplayToolCaller(session);
  assert.deepEqual(tools.call("search", { q: "foo" }), [1, 2, 3]);
  assert.throws(() => new ReplayToolCaller(session).call("other", { q: "foo" }), ReplayDivergence);
});

test("replaySession re-executes deterministic local agents and detects workspace divergence", async () => {
  const previousReplaySigning = process.env.A2A_REPLAY_SIGNING_KEY;
  const previousReplayVerify = process.env.A2A_REPLAY_VERIFYING_KEY;
  const replayKeys = generateKeyPairSync("ed25519", {
    publicKeyEncoding: { type: "spki", format: "pem" },
    privateKeyEncoding: { type: "pkcs8", format: "pem" }
  });
  try {
    process.env.A2A_REPLAY_SIGNING_KEY = replayKeys.privateKey;
    process.env.A2A_REPLAY_VERIFYING_KEY = replayKeys.publicKey;
    const agent = new ReplayAgent();
    const response = await agent.dispatch({
      agent: "replay-agent",
      skill: "draws",
      handler: "draws",
      arguments: { n: 5 },
      random_seed: "seed-replay"
    });
    const replayEvent = response.events.find((event) => event.kind === "replay_sealed");
    assert.ok(replayEvent);
    const session = verifyReplaySession(replayEvent.payload.token);
    assert.deepEqual(await replaySession(agent, session), response.result);
    await assert.rejects(() => replaySession(new ReplayMutantAgent(), session), ReplayDivergence);
  } finally {
    restoreEnv("A2A_REPLAY_SIGNING_KEY", previousReplaySigning);
    restoreEnv("A2A_REPLAY_VERIFYING_KEY", previousReplayVerify);
  }
});

test("RunContext requireScopes reports missing scopes", () => {
  const ctx = new RunContext({
    agent: "math-agent",
    skill: "context_sum",
    handler: "contextSumHandler",
    arguments: {},
    auth: { scope: "math:read" }
  });
  assert.throws(() => ctx.requireScopes(["math:add"]), MissingScopes);
});

test("RunContext exposes caller grant ids deterministic random and cancellation", async () => {
  const ctxA = new RunContext({
    agent: "math-agent",
    skill: "context_sum",
    handler: "contextSumHandler",
    arguments: {},
    caller: { agent: "main-agent" },
    grant_ids: ["grant-1"],
    random_seed: "seed-1"
  });
  const ctxB = new RunContext({
    agent: "math-agent",
    skill: "context_sum",
    handler: "contextSumHandler",
    arguments: {},
    random_seed: "seed-1"
  });

  assert.deepEqual(ctxA.caller, { agent: "main-agent" });
  assert.deepEqual(ctxA.grantIds, ["grant-1"]);
  assert.equal(ctxA.random(), ctxB.random());

  const cancelled = new RunContext({
    agent: "math-agent",
    skill: "context_sum",
    handler: "contextSumHandler",
    arguments: {},
    cancelled: true
  });
  await assert.rejects(() => cancelled.checkCancelled(), CancelledByCaller);
});

test("RunContext consumerSecret uses Python-compatible missing setup error", () => {
  const ctx = new RunContext({
    agent: "math-agent",
    skill: "context_sum",
    handler: "contextSumHandler",
    arguments: {}
  });
  assert.throws(() => ctx.consumerSecret("TOKEN"), ConsumerSetupMissing);
});

test("RunContext llm normalizes forwarded caller credentials", () => {
  const ctx = new RunContext({
    agent: "math-agent",
    skill: "context_sum",
    handler: "contextSumHandler",
    arguments: {},
    llm_creds: {
      base_url: "http://litellm:4000/v1",
      api_key: "user-provider-key",
      model: "user-selected-model",
      extra_body: null
    }
  });

  assert.deepEqual(ctx.llm(), {
    base_url: "http://litellm:4000/v1",
    api_key: "user-provider-key",
    model: "user-selected-model",
    source: "caller",
    temperature_mode: "default",
    temperature: null,
    extra_body: {},
    metadata: {}
  });
  assert.equal(ctx.llm.model, "user-selected-model");
});

test("RunContext llm infers platform source for forwarded grant credentials", () => {
  const grant = "grant-token";
  const ctx = new RunContext({
    agent: "math-agent",
    skill: "context_sum",
    handler: "contextSumHandler",
    arguments: {},
    grant,
    llm_creds: {
      base_url: "http://litellm:4000/v1",
      api_key: grant,
      model: "platform-model",
      temperature_mode: "omit",
      temperature: "0.2",
      extra_body: { extra_body: { thinking: { type: "disabled" } } }
    }
  });

  assert.deepEqual(ctx.llm(), {
    base_url: "http://litellm:4000/v1",
    api_key: grant,
    model: "platform-model",
    source: "platform",
    temperature_mode: "omit",
    temperature: 0.2,
    extra_body: { extra_body: { thinking: { type: "disabled" } } },
    metadata: {}
  });
});

test("RunContext llm falls back to agent BYOK env before platform env", () => {
  const previousAgentKey = process.env.AGENT_LLM_KEY;
  const previousAgentUrl = process.env.AGENT_LLM_URL;
  const previousAgentModel = process.env.AGENT_LLM_MODEL;
  const previousPlatformKey = process.env.A2A_LITELLM_KEY;
  try {
    process.env.AGENT_LLM_KEY = "agent-key";
    process.env.AGENT_LLM_URL = "https://agent-llm.example.test/v1";
    process.env.AGENT_LLM_MODEL = "agent-model";
    process.env.A2A_LITELLM_KEY = "platform-key";
    const ctx = new RunContext({
      agent: "math-agent",
      skill: "context_sum",
      handler: "contextSumHandler",
      arguments: {}
    });

    assert.deepEqual(ctx.llm(), {
      base_url: "https://agent-llm.example.test/v1",
      api_key: "agent-key",
      model: "agent-model",
      source: "agent_byok",
      temperature_mode: "default",
      temperature: null,
      extra_body: {},
      metadata: {}
    });
  } finally {
    restoreEnv("AGENT_LLM_KEY", previousAgentKey);
    restoreEnv("AGENT_LLM_URL", previousAgentUrl);
    restoreEnv("AGENT_LLM_MODEL", previousAgentModel);
    restoreEnv("A2A_LITELLM_KEY", previousPlatformKey);
  }
});

test("RunContext llm falls back to platform LiteLLM defaults", () => {
  const previousAgentKey = process.env.AGENT_LLM_KEY;
  const previousAgentUrl = process.env.AGENT_LLM_URL;
  const previousAgentModel = process.env.AGENT_LLM_MODEL;
  const previousPlatformKey = process.env.A2A_LITELLM_KEY;
  const previousPlatformUrl = process.env.A2A_LITELLM_URL;
  const previousPlatformModel = process.env.A2A_LITELLM_MODEL;
  try {
    delete process.env.AGENT_LLM_KEY;
    delete process.env.AGENT_LLM_URL;
    delete process.env.AGENT_LLM_MODEL;
    process.env.A2A_LITELLM_KEY = "platform-key";
    process.env.A2A_LITELLM_URL = "http://platform-litellm:4000";
    process.env.A2A_LITELLM_MODEL = "platform-model";
    const ctx = new RunContext({
      agent: "math-agent",
      skill: "context_sum",
      handler: "contextSumHandler",
      arguments: {}
    });

    assert.deepEqual(ctx.llm(), {
      base_url: "http://platform-litellm:4000/v1",
      api_key: "platform-key",
      model: "platform-model",
      source: "platform",
      temperature_mode: "default",
      temperature: null,
      extra_body: {},
      metadata: {}
    });
  } finally {
    restoreEnv("AGENT_LLM_KEY", previousAgentKey);
    restoreEnv("AGENT_LLM_URL", previousAgentUrl);
    restoreEnv("AGENT_LLM_MODEL", previousAgentModel);
    restoreEnv("A2A_LITELLM_KEY", previousPlatformKey);
    restoreEnv("A2A_LITELLM_URL", previousPlatformUrl);
    restoreEnv("A2A_LITELLM_MODEL", previousPlatformModel);
  }
});

test("RunContext ask and collect wait for callback submissions", async () => {
  const ctx = new RunContext({
    agent: "math-agent",
    skill: "context_sum",
    handler: "contextSumHandler",
    arguments: {}
  });

  const answerPromise = ctx.ask("Continue?", { timeoutSeconds: 2 });
  const questionEvent = ctx.emittedEvents.find((event) => event.kind === "question");
  assert.ok(questionEvent);
  assert.equal(questionEvent.payload.prompt, "Continue?");
  assert.equal(RunContext.answer(questionEvent.payload.question_id, "yes"), true);
  assert.equal(await answerPromise, "yes");

  const inputPromise = ctx.collect({ type: "object", properties: { title: { type: "string" } } }, {
    title: "Video setup",
    timeoutSeconds: 2
  });
  const inputEvent = ctx.emittedEvents.find((event) => event.kind === "input_request");
  assert.ok(inputEvent);
  assert.equal(inputEvent.payload.title, "Video setup");
  assert.equal(RunContext.submitInput(inputEvent.payload.request_id, { title: "A2A" }), true);
  assert.deepEqual(await inputPromise, { title: "A2A" });

  const invalidPromise = ctx.collect({
    type: "object",
    properties: { title: { type: "string" } },
    required: ["title"]
  }, { timeoutSeconds: 2 });
  const invalidEvent = ctx.emittedEvents.filter((event) => event.kind === "input_request").at(-1);
  assert.equal(RunContext.submitInput(invalidEvent.payload.request_id, {}), true);
  await assert.rejects(() => invalidPromise, /input.title is required/);

  const constrainedPromise = ctx.collect({
    type: "object",
    properties: {
      title: { type: "string", minLength: 3, pattern: "^[A-Z]" },
      count: { type: "integer", minimum: 1 },
      tags: { type: "array", minItems: 1, uniqueItems: true, items: { type: "string" } }
    },
    required: ["title", "count", "tags"]
  }, { timeoutSeconds: 2 });
  const constrainedEvent = ctx.emittedEvents.filter((event) => event.kind === "input_request").at(-1);
  assert.equal(RunContext.submitInput(constrainedEvent.payload.request_id, {
    title: "ab",
    count: 0,
    tags: ["x", "x"]
  }), true);
  await assert.rejects(() => constrainedPromise, /input.title must have at least 3 characters/);
});

test("RunContext requestScope waits for grant callback and refreshes workspace", async () => {
  const ctx = new RunContext({
    agent: "math-agent",
    skill: "context_sum",
    handler: "contextSumHandler",
    arguments: {},
    cp_url: "http://control-plane.example.test",
    scope_expansion_allowed: true
  });

  const scopePromise = ctx.requestScope({
    read: "agents/demo/**",
    write: "outputs/",
    reason: "Need workspace output"
  });
  const scopeEvent = ctx.emittedEvents.find((event) => event.kind === "scope_request");
  assert.ok(scopeEvent);
  assert.deepEqual(scopeEvent.payload.read_prefixes, ["agents/demo/**"]);
  assert.deepEqual(scopeEvent.payload.write_prefixes, ["outputs/"]);
  const grant = grantToken({
    bucket: "user-2-files",
    mode: "read_write_overlay",
    outputs_prefix: "outputs/",
    write_prefixes: ["outputs/"]
  });
  assert.equal(RunContext.resolveScopeGrant(scopeEvent.payload.request_id, { grant }), true);
  assert.deepEqual(await scopePromise, {
    bucket: "user-2-files",
    mode: "read_write_overlay",
    outputs_prefix: "outputs/",
    write_prefixes: ["outputs/"]
  });
  assert.equal(ctx.workspace.bucket, "user-2-files");
});

test("RunContext writeArtifact falls back to memory artifact without workspace grant", async () => {
  const ctx = new RunContext({
    agent: "math-agent",
    skill: "context_sum",
    handler: "contextSumHandler",
    arguments: {},
    task_id: "task-1"
  });

  const ref = await ctx.writeArtifact("notes/result.txt", "hello", "text/plain");

  assert.deepEqual(ref, {
    name: "notes/result.txt",
    path: "outputs/task-1/notes/result.txt",
    uri: "memory://task-1/notes/result.txt",
    mime_type: "text/plain",
    content_type: "text/plain",
    bytes: 5,
    metadata: {}
  });
  assert.deepEqual(ctx.emittedArtifacts, [ref]);
});

test("RunContext writeArtifact persists through control-plane workspace grants", async () => {
  const calls = [];
  const server = createServer(async (req, res) => {
    const chunks = [];
    for await (const chunk of req) chunks.push(Buffer.from(chunk));
    calls.push({
      method: req.method,
      url: req.url,
      grant: req.headers["x-a2a-grant"],
      contentType: req.headers["content-type"],
      body: Buffer.concat(chunks).toString("utf8")
    });
    res.writeHead(204).end();
  });
  after(() => server.close());
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  assert.equal(typeof address, "object");
  const cpUrl = `http://127.0.0.1:${address.port}`;
  const grant = grantToken({
    bucket: "user-2-files",
    outputs_prefix: "outputs/",
    mode: "read_write_overlay"
  });
  const ctx = new RunContext({
    agent: "math-agent",
    skill: "context_sum",
    handler: "contextSumHandler",
    arguments: {},
    task_id: "run-42",
    grant,
    cp_url: cpUrl
  });

  const ref = await ctx.writeArtifact("developer-video.mp4", Buffer.from("video"), "video/mp4");

  assert.deepEqual(calls, [
    {
      method: "PUT",
      url: "/v1/workspace-grants/files/outputs/run-42/developer-video.mp4",
      grant,
      contentType: "application/octet-stream",
      body: "video"
    }
  ]);
  assert.deepEqual(ref, {
    name: "developer-video.mp4",
    path: "outputs/run-42/developer-video.mp4",
    uri: "s3://user-2-files/outputs/run-42/developer-video.mp4",
    mime_type: "video/mp4",
    content_type: "video/mp4",
    bytes: 5,
    metadata: {}
  });
  assert.equal(ctx.workspace.outputsPrefix, "outputs/");
});

test("RunContext memory persists notes and logs through workspace grants", async () => {
  const files = new Map();
  const server = createServer(async (req, res) => {
    const rawPath = req.url.replace("/v1/workspace-grants/files", "").replace(/^\/+/, "");
    const path = decodeURIComponent(rawPath);
    if (req.method === "HEAD") {
      res.writeHead(files.has(path) ? 204 : 404).end();
      return;
    }
    if (req.method === "GET" && path === "") {
      res.writeHead(200, { "content-type": "application/json" }).end(JSON.stringify(
        [...files.entries()].map(([filePath, content]) => ({ path: filePath, size_bytes: Buffer.byteLength(content) }))
      ));
      return;
    }
    if (req.method === "GET") {
      const content = files.get(path);
      if (content === undefined) {
        res.writeHead(404).end();
        return;
      }
      res.writeHead(200, { "content-type": "application/octet-stream" }).end(content);
      return;
    }
    if (req.method === "PUT") {
      const chunks = [];
      for await (const chunk of req) chunks.push(Buffer.from(chunk));
      files.set(path, Buffer.concat(chunks).toString("utf8"));
      res.writeHead(204).end();
      return;
    }
    res.writeHead(405).end();
  });
  after(() => server.close());
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  assert.equal(typeof address, "object");
  const ctx = new RunContext({
    agent: "math-agent",
    skill: "context_sum",
    handler: "contextSumHandler",
    arguments: {},
    caller: { sub: "user-2" },
    cp_url: `http://127.0.0.1:${address.port}`,
    grant: grantToken({
      bucket: "user-2-files",
      mode: "read_write_direct",
      allow_patterns: ["**"]
    })
  });

  const record = await ctx.memory.remember("topic", { value: "A2A TypeScript parity" });
  assert.equal(record.key, "topic");
  assert.equal(record.namespace, "notes");
  assert.deepEqual((await ctx.memory.recall("topic")).value, { value: "A2A TypeScript parity" });
  assert.equal((await ctx.memory.search("parity")).length, 1);
  await ctx.memory.appendLog("events", { ok: true });
  assert.deepEqual((await ctx.memory.readLog("events"))[0].value, { ok: true });
  assert.ok(files.has("memory/math-agent/user-2/notes/topic.json"));
  assert.ok(files.has("memory/math-agent/user-2/logs/events.jsonl"));
});

test("RunContext workspace search and view mirror Python helper surface", async () => {
  const files = new Map([
    ["docs/readme.txt", "hello workspace"],
    ["docs/guide.md", "guide"]
  ]);
  const server = createServer(async (req, res) => {
    const rawPath = req.url.replace("/v1/workspace-grants/files", "").replace(/^\/+/, "");
    const path = decodeURIComponent(rawPath);
    if (req.method === "GET" && path === "") {
      res.writeHead(200, { "content-type": "application/json" }).end(JSON.stringify(
        [...files.keys()].map((filePath) => ({ path: filePath, size_bytes: files.get(filePath).length }))
      ));
      return;
    }
    if (req.method === "GET") {
      res.writeHead(200).end(files.get(path));
      return;
    }
    res.writeHead(204).end();
  });
  after(() => server.close());
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  assert.equal(typeof address, "object");
  const ctx = new RunContext({
    agent: "math-agent",
    skill: "context_sum",
    handler: "contextSumHandler",
    arguments: {},
    cp_url: `http://127.0.0.1:${address.port}`,
    grant: grantToken({ bucket: "user-2-files", mode: "read_only", allow_patterns: ["**"] })
  });

  assert.deepEqual((await ctx.workspace.search("guide")).map((item) => item.path), ["docs/guide.md"]);
  assert.deepEqual(await ctx.workspace.view("docs/readme.txt", { maxBytes: 5 }), {
    path: "docs/readme.txt",
    content: "hello",
    truncated: true,
    bytes: 15
  });
});

test("RunContext control-plane helper clients mirror Python endpoint contracts", async () => {
  const calls = [];
  const server = createServer(async (req, res) => {
    const chunks = [];
    for await (const chunk of req) chunks.push(Buffer.from(chunk));
    calls.push({
      method: req.method,
      url: req.url,
      authorization: req.headers.authorization,
      body: chunks.length ? JSON.parse(Buffer.concat(chunks).toString("utf8")) : null
    });
    if (req.url === "/v1/agents/math-agent/meta-runs" && req.method === "POST") {
      res.writeHead(200, { "content-type": "application/json" }).end(JSON.stringify({ run_id: "run-1" }));
      return;
    }
    if (req.url === "/v1/agents/math-agent/protocol-simulations/scenarios" && req.method === "GET") {
      res.writeHead(200, { "content-type": "application/json" }).end(JSON.stringify([{ scenario_id: "s1" }]));
      return;
    }
    if (req.url === "/v1/agents/math-agent/protocol-simulations/runtime-readiness" && req.method === "POST") {
      res.writeHead(200, { "content-type": "application/json" }).end(JSON.stringify({ allowed: true }));
      return;
    }
    res.writeHead(404).end();
  });
  after(() => server.close());
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  assert.equal(typeof address, "object");
  const ctx = new RunContext({
    agent: "math-agent",
    skill: "context_sum",
    handler: "contextSumHandler",
    arguments: {},
    cp_url: `http://127.0.0.1:${address.port}`,
    cp_jwt: "cp-token"
  });

  assert.deepEqual(await ctx.metaRuns.create({ goal: "ship parity" }), { run_id: "run-1" });
  assert.deepEqual(await ctx.protocolSimulations.listScenarios(), [{ scenario_id: "s1" }]);
  assert.deepEqual(await ctx.protocol_simulations.check_runtime_readiness({ simulation_passed: true }), { allowed: true });
  assert.deepEqual(calls.map((call) => [call.method, call.url, call.authorization]), [
    ["POST", "/v1/agents/math-agent/meta-runs", "Bearer cp-token"],
    ["GET", "/v1/agents/math-agent/protocol-simulations/scenarios", "Bearer cp-token"],
    ["POST", "/v1/agents/math-agent/protocol-simulations/runtime-readiness", "Bearer cp-token"]
  ]);
  assert.equal(calls[2].body.simulation_passed, true);
});

test("RunContext discovery and subagent toolkit use control-plane agent registry", async () => {
  const server = createServer(async (req, res) => {
    const base = `http://127.0.0.1:${server.address().port}`;
    if (req.url.startsWith("/v1/agents?")) {
      res.writeHead(200, { "content-type": "application/json" }).end(JSON.stringify([
        { name: "helper-agent" }
      ]));
      return;
    }
    if (req.url === "/v1/agents/helper-agent") {
      res.writeHead(200, { "content-type": "application/json" }).end(JSON.stringify({
        name: "helper-agent",
        url: base,
        card: {
          description: "Helper",
          version: "0.1.0",
          capabilities: { search: true },
          skills: [{ name: "lookup", description: "Lookup", tags: ["search"], input_schema: { type: "object" } }]
        }
      }));
      return;
    }
    if (req.url === "/invoke/lookup" && req.method === "POST") {
      res.writeHead(200, { "content-type": "application/json" }).end(JSON.stringify({
        result: { ok: true },
        events: [{ kind: "progress", payload: { message: "called" } }],
        artifacts: [{ name: "result.json" }]
      }));
      return;
    }
    res.writeHead(404).end();
  });
  after(() => server.close());
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  assert.equal(typeof address, "object");
  const ctx = new RunContext({
    agent: "math-agent",
    skill: "context_sum",
    handler: "contextSumHandler",
    arguments: {},
    cp_url: `http://127.0.0.1:${address.port}`,
    cp_jwt: "cp-token"
  });

  const listed = await ctx.subagents.listSubagents({ skill: "lookup" });
  assert.deepEqual(listed[0].skills[0].name, "lookup");
  assert.deepEqual(await ctx.subagents.callSubagent("helper-agent", "lookup", { args: { q: "a2a" } }), {
    ok: true,
    agent: "helper-agent",
    skill: "lookup",
    result: { ok: true },
    events: [{ kind: "progress", payload: { message: "called" } }],
    artifacts: [{ name: "result.json" }],
    grant_id: null
  });
});

test("A2A clients support HTTP and in-memory multi-agent routing", async () => {
  const childGrant = grantToken({ grant_id: "child-1" });
  class SetupEchoAgent extends A2AAgent {
    static agent = {
      name: "setup-echo",
      description: "Echoes caller setup"
    };
    static skills = [
      skill({
        name: "read_setup",
        handler: "readSetup",
        input_schema: {
          type: "object",
          properties: {},
          required: [],
          additionalProperties: false
        },
        output_schema: {
          type: "object",
          properties: {
            plan: { type: "string" },
            token: { type: "string" }
          },
          required: ["plan", "token"],
          additionalProperties: false
        }
      })
    ];

    async readSetup(ctx, _input) {
      return {
        plan: ctx.consumerConfig("PLAN"),
        token: ctx.consumerSecret("API_TOKEN")
      };
    }
  }
  const inMemory = new InMemoryA2AClient({
    agents: {
      greeter: new ConfigLifecycleAgent({ prefix: "hi" }),
      setup: new SetupEchoAgent()
    },
    ctxFactory: (_agent, grant) => ({
      grant,
      auth: { scopes: [] }
    })
  });

  assert.deepEqual(await inMemory.call("greeter", "greet", {
    args: { who: "Lin" },
    grant: childGrant
  }), {
    result: { message: "hi Lin", retries: 2 },
    events: [{ kind: "progress", payload: { message: "greeting" } }],
    artifacts: [],
    grant_id: "child-1"
  });

  const ctx = new RunContext({
    agent: "math-agent",
    skill: "context_sum",
    handler: "contextSumHandler",
    arguments: {},
    a2a_client: inMemory
  });
  assert.deepEqual(await ctx.call("greeter", "greet", { who: "Jo" }, { grant: childGrant }), {
    result: { message: "hi Jo", retries: 2 },
    events: [{ kind: "progress", payload: { message: "greeting" } }],
    artifacts: [],
    grant_id: "child-1"
  });
  assert.deepEqual(await inMemory.call("setup", "read_setup", {
    consumer_config: { PLAN: "pro" },
    consumer_secrets: { API_TOKEN: "tok-inline" }
  }), {
    result: { plan: "pro", token: "tok-inline" },
    events: [],
    artifacts: [],
    grant_id: null
  });

  let httpBody = null;
  let httpHeaders = null;
  const server = createServer(async (req, res) => {
    assert.equal(req.url, "/invoke/greet");
    httpHeaders = req.headers;
    let raw = "";
    for await (const chunk of req) raw += chunk;
    httpBody = JSON.parse(raw);
    res.writeHead(200, { "content-type": "application/json" }).end(JSON.stringify({
      result: { message: "remote Ada", retries: 1 },
      events: [],
      artifacts: []
    }));
  });
  after(() => server.close());
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  assert.equal(typeof address, "object");
  const http = new HttpA2AClient({
    discovery: {
      async getAgent(name) {
        return { name, url: `http://127.0.0.1:${address.port}`, card: {} };
      }
    }
  });
  assert.deepEqual(await http.call("greeter", "greet", {
    args: { who: "Ada" },
    cp_jwt: "user-cp-jwt",
    cp_url: "http://cp.local",
    consumer_config: { PLAN: "enterprise" },
    consumer_secrets: { API_TOKEN: "tok-http" }
  }), {
    result: { message: "remote Ada", retries: 1 },
    events: [],
    artifacts: [],
    grant_id: null
  });
  assert.equal(httpHeaders.authorization, "bearer user-cp-jwt");
  assert.equal(httpBody.cp_jwt, "user-cp-jwt");
  assert.equal(httpBody.cp_url, "http://cp.local");
  assert.deepEqual(httpBody.consumer_config, { PLAN: "enterprise" });
  assert.deepEqual(httpBody.consumer_secrets, { API_TOKEN: "tok-http" });
});

test("RunContext call returns Python-compatible CallResult and workspace delegates grants", async () => {
  const calls = [];
  const server = createServer(async (req, res) => {
    const chunks = [];
    for await (const chunk of req) chunks.push(Buffer.from(chunk));
    calls.push(JSON.parse(Buffer.concat(chunks).toString("utf8")));
    res.writeHead(200, { "content-type": "application/json" }).end(JSON.stringify({
      result: { ok: true },
      events: [{ kind: "progress", payload: { message: "done" } }],
      artifacts: [{ name: "out.txt" }]
    }));
  });
  after(() => server.close());
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  assert.equal(typeof address, "object");
  const parentGrant = grantToken({
    grant_id: "parent-grant",
    issuer: "self:user-2",
    bucket: "user-2-files",
    mode: "read_write_overlay",
    allow_patterns: ["agents/demo/**"],
    deny_patterns: ["secrets/**"],
    outputs_prefix: "outputs/",
    write_prefixes: ["outputs/"],
    delegation_depth: 0
  });
  const ctx = new RunContext({
    agent: "math-agent",
    skill: "context_sum",
    handler: "contextSumHandler",
    arguments: {},
    cp_url: `http://127.0.0.1:${address.port}`,
    grant: parentGrant
  });

  const childGrant = await ctx.workspace.delegate({
    audience: "helper-agent",
    allow_patterns: ["agents/demo/**"],
    outputs_prefix: "outputs/child/",
    write_prefixes: ["outputs/child/"]
  });
  const childPayload = JSON.parse(Buffer.from(childGrant.split(".")[0], "base64url").toString("utf8"));
  assert.equal(childPayload.parent_grant_id, "parent-grant");
  assert.equal(childPayload.delegation_depth, 1);
  assert.deepEqual(childPayload.deny_patterns, ["secrets/**"]);
  assert.deepEqual(childPayload.write_prefixes, ["outputs/child/"]);

  const result = await ctx.call(`http://127.0.0.1:${address.port}`, "lookup", { q: "a2a" }, { grant: childGrant });
  assert.deepEqual(result, {
    result: { ok: true },
    events: [{ kind: "progress", payload: { message: "done" } }],
    artifacts: [{ name: "out.txt" }],
    grant_id: childPayload.grant_id
  });
  assert.equal(calls[0].grant, childGrant);
  assert.deepEqual(ctx.emittedEvents.filter((event) => event.kind.startsWith("composition_call")).map((event) => event.kind), [
    "composition_call_started",
    "composition_call_complete"
  ]);
});

test("signed grant primitives and workspace delegation mirror Python bounds", async () => {
  const previousSigning = process.env.A2A_GRANT_SIGNING_KEY;
  const previousVerify = process.env.A2A_GRANT_VERIFYING_KEY;
  const keys = generateKeyPairSync("ed25519", {
    publicKeyEncoding: { type: "spki", format: "pem" },
    privateKeyEncoding: { type: "pkcs8", format: "pem" }
  });
  try {
    process.env.A2A_GRANT_SIGNING_KEY = keys.privateKey;
    process.env.A2A_GRANT_VERIFYING_KEY = keys.publicKey;
    const [parent, parentToken] = mintGrant({
      issuer: "main-agent",
      audience: "worker-agent",
      bucket: "user-2-files",
      mode: "read_write_overlay",
      allow_patterns: ["agents/demo/**"],
      outputs_prefix: "outputs/",
      write_prefixes: ["outputs/video/"]
    });
    assert.equal(verifyGrant(parentToken).bucket, "user-2-files");

    const ctx = new RunContext({
      agent: "math-agent",
      skill: "context_sum",
      handler: "contextSumHandler",
      arguments: {},
      cp_url: "http://control-plane.example.test",
      grant: parentToken
    });
    const childToken = await ctx.workspace.delegate({
      audience: "render-agent",
      allow_patterns: ["agents/demo/**"],
      outputs_prefix: "outputs/video/render/",
      write_prefixes: ["outputs/video/render/"]
    });
    const child = verifyGrant(childToken);
    assert.equal(child.parent_grant_id, parent.grant_id);
    assert.equal(child.delegation_depth, 1);
    assert.deepEqual(child.write_prefixes, ["outputs/video/render/"]);

    await assert.rejects(
      () => ctx.workspace.delegate({
        audience: "bad-agent",
        allow_patterns: ["agents/demo/**"],
        outputs_prefix: "private/",
        write_prefixes: ["private/"]
      }),
      GrantDelegationDenied
    );
  } finally {
    restoreEnv("A2A_GRANT_SIGNING_KEY", previousSigning);
    restoreEnv("A2A_GRANT_VERIFYING_KEY", previousVerify);
  }
});

test("RunContext exposes workspace/deepagents backend compatibility surface", () => {
  const ctx = new RunContext({
    agent: "math-agent",
    skill: "context_sum",
    handler: "contextSumHandler",
    arguments: {},
    cp_url: "http://control-plane.example.test",
    grant: grantToken({
      bucket: "user-2-files",
      mode: "read_write_overlay",
      outputs_prefix: "outputs/",
      write_prefixes: ["outputs/video/"]
    })
  });
  const backend = ctx.deepagents_backend();
  assert.equal(backend.artifacts_root, "/outputs/video/.a2a-artifacts");
  assert.equal(backend.workspace.bucket, "user-2-files");
  assert.deepEqual(backend.tools().map((tool) => tool.name), ["memory", "subagents"]);
});

test("RunContext sandbox mirrors Python HTTP sandbox grant contract", async () => {
  const previousUrl = process.env.A2A_SANDBOX_URL;
  const previousToken = process.env.A2A_SANDBOX_TOKEN;
  const previousTimeout = process.env.A2A_SANDBOX_TIMEOUT_S;
  const calls = [];
  const server = createServer(async (req, res) => {
    const chunks = [];
    for await (const chunk of req) chunks.push(Buffer.from(chunk));
    calls.push({
      method: req.method,
      url: req.url,
      authorization: req.headers.authorization,
      grant: req.headers["x-a2a-grant"],
      body: JSON.parse(Buffer.concat(chunks).toString("utf8"))
    });
    res.writeHead(200, { "content-type": "application/json" }).end(JSON.stringify({
      stdout: "ok\n",
      stderr: "",
      exit_code: 0,
      files: [{ path: "outputs/result.txt" }]
    }));
  });
  after(() => {
    server.close();
    restoreEnv("A2A_SANDBOX_URL", previousUrl);
    restoreEnv("A2A_SANDBOX_TOKEN", previousToken);
    restoreEnv("A2A_SANDBOX_TIMEOUT_S", previousTimeout);
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  assert.equal(typeof address, "object");
  process.env.A2A_SANDBOX_URL = `http://127.0.0.1:${address.port}`;
  process.env.A2A_SANDBOX_TOKEN = "sandbox-token";
  process.env.A2A_SANDBOX_TIMEOUT_S = "77";
  const grant = grantToken({ bucket: "user-2-files" });
  const ctx = new RunContext({
    agent: "math-agent",
    skill: "context_sum",
    handler: "contextSumHandler",
    arguments: {},
    grant
  });

  const result = await ctx.sandbox.runShell("echo ok");

  assert.deepEqual(result, {
    stdout: "ok\n",
    stderr: "",
    exit_code: 0,
    truncated: false,
    files: [{ path: "outputs/result.txt" }]
  });
  assert.deepEqual(calls, [
    {
      method: "POST",
      url: "/v1/run_shell",
      authorization: "Bearer sandbox-token",
      grant,
      body: {
        script: "echo ok",
        image: "python:3.11-slim",
        bucket: "user-2-files",
        timeout_seconds: 77
      }
    }
  ]);
});

test("RunContext sandbox reports missing runtime", () => {
  const previousUrl = process.env.A2A_SANDBOX_URL;
  delete process.env.A2A_SANDBOX_URL;
  try {
    const ctx = new RunContext({
      agent: "math-agent",
      skill: "context_sum",
      handler: "contextSumHandler",
      arguments: {}
    });
    assert.throws(() => ctx.sandbox, SandboxUnavailable);
  } finally {
    restoreEnv("A2A_SANDBOX_URL", previousUrl);
  }
});

test("serveAgent streams emitted events before final result", async () => {
  const server = serveAgent(MathAgent, { port: 0 });
  after(() => server.close());

  await new Promise((resolve) => server.once("listening", resolve));
  const address = server.address();
  assert.equal(typeof address, "object");
  const url = `http://127.0.0.1:${address.port}/_a2a/invoke-stream/sumHandler`;
  const response = await fetch(url, {
    method: "POST",
    headers: { "content-type": "application/json", accept: "text/event-stream" },
    body: JSON.stringify({
      agent: "math-agent",
      skill: "sum",
      handler: "sumHandler",
      arguments: { left: 4, right: 5 }
    })
  });

  assert.equal(response.status, 200);
  assert.match(response.headers.get("content-type") ?? "", /text\/event-stream/);
  const frames = await readSseFrames(response);
  assert.deepEqual(frames[0], { type: "started" });
  assert.deepEqual(frames[1], {
    type: "event",
    kind: "progress",
    payload: { step: "sum" }
  });
  assert.deepEqual(frames[2], {
    type: "result",
    result: 9,
    events: [{ kind: "tested" }],
    artifacts: []
  });
  assert.equal(frames[3], "[DONE]");
});

test("skill rejects invalid request schema", () => {
  assert.throws(
    () =>
      skill({
        name: "bad",
        input_schema: { type: "string" },
        output_schema: { type: "string" }
      }),
    /input_schema must be an object schema/
  );
});

async function readSseFrames(response) {
  const text = await response.text();
  return text
    .split("\n")
    .filter((line) => line.startsWith("data: "))
    .map((line) => {
      const raw = line.slice("data: ".length);
      return raw === "[DONE]" ? raw : JSON.parse(raw);
    });
}

function grantToken(payload) {
  const encoded = Buffer.from(JSON.stringify(payload))
    .toString("base64url");
  return `${encoded}.signature`;
}

function restoreEnv(name, value) {
  if (value === undefined) {
    delete process.env[name];
  } else {
    process.env[name] = value;
  }
}
