#!/usr/bin/env node
/**
 * `a2amcp` — install once, expose any number of a2acloud agents to your MCP
 * client (Claude Code, Cursor, etc.).
 *
 * No-argument invocation runs the gateway over stdio. That's the form an MCP
 * client launches. Subcommands manage the local agent list, auth, etc.
 */
import { createRequire } from "node:module";
import { Command } from "commander";
import readline from "node:readline/promises";
import { stdin as input, stdout as output, stderr } from "node:process";

const require = createRequire(import.meta.url);
const pkg = require("../package.json") as { version: string };

import { ApiError, ControlPlaneClient } from "./api.js";
import { loadConfig, removeAgent } from "./config.js";
import {
  DEFAULT_API_URL,
  clearCredentials,
  loadCredentials,
  resolveApiUrl,
} from "./credentials.js";
import { runStdio } from "./gateway.js";
import {
  DEFAULT_OAUTH_CLIENT_ID,
  DEFAULT_OAUTH_ISSUER,
  DEFAULT_OAUTH_SCOPE,
  DEFAULT_REDIRECT_PORT,
  loginWithAccessToken,
  loginWithBrowser,
  refreshCredentialsIfNeeded,
} from "./oauth.js";
import {
  enableAgentByName,
  searchVisibleAgents,
  summarizeAgent,
} from "./registry.js";

const program = new Command();

program
  .name("a2amcp")
  .description("MCP gateway for a2acloud agents.")
  .version(pkg.version);

async function client(apiOverride?: string): Promise<ControlPlaneClient> {
  const creds = await loadCredentials();
  const refreshed = creds ? await refreshCredentialsIfNeeded(creds) : null;
  const apiUrl = resolveApiUrl(apiOverride ?? creds?.apiUrl);
  return new ControlPlaneClient(apiUrl, refreshed?.token ?? null);
}

function fail(msg: string, code = 1): never {
  stderr.write(`error: ${msg}\n`);
  process.exit(code);
}

async function prompt(label: string, opts: { hidden?: boolean } = {}): Promise<string> {
  const rl = readline.createInterface({ input, output, terminal: true });
  try {
    if (opts.hidden) {
      // readline doesn't natively hide; this is a best-effort hide.
      const orig = (rl as any)._writeToOutput;
      (rl as any)._writeToOutput = (s: string) => {
        if (s.includes(label)) (output as any).write(label);
        else (output as any).write("*");
      };
      const answer = await rl.question(label);
      (rl as any)._writeToOutput = orig;
      output.write("\n");
      return answer;
    }
    return await rl.question(label);
  } finally {
    rl.close();
  }
}

// --- run (default) --------------------------------------------------------- #

program
  .command("run", { isDefault: true, hidden: true })
  .description("Start the MCP gateway on stdio (invoked by your MCP client).")
  .action(async () => {
    try {
      await runStdio();
    } catch (err) {
      stderr.write(`a2amcp: ${(err as Error).message}\n`);
      process.exit(1);
    }
  });

// --- auth ------------------------------------------------------------------ #

program
  .command("login")
  .description("Authenticate with Keycloak and cache a bearer token.")
  .option("--api <url>", "Override control plane URL", DEFAULT_API_URL)
  .option("--issuer <url>", "Keycloak realm issuer", DEFAULT_OAUTH_ISSUER)
  .option("--client-id <id>", "Keycloak public client id", DEFAULT_OAUTH_CLIENT_ID)
  .option("--scope <scope>", "OAuth scopes to request", DEFAULT_OAUTH_SCOPE)
  .option("--port <port>", "Loopback callback port", String(DEFAULT_REDIRECT_PORT))
  .option("--token <token>", "Save an already-issued Keycloak access token")
  .option("--no-open", "Print the login URL without opening a browser")
  .action(async (opts: {
    api: string;
    issuer: string;
    clientId: string;
    scope: string;
    port: string;
    token?: string;
    open?: boolean;
  }) => {
    const api = resolveApiUrl(opts.api);
    try {
      const common = {
        apiUrl: api,
        issuer: opts.issuer,
        clientId: opts.clientId,
        scope: opts.scope,
      };
      const creds = opts.token
        ? await loginWithAccessToken(opts.token, common)
        : await loginWithBrowser({
            ...common,
            port: Number.parseInt(opts.port, 10),
            openBrowser: opts.open !== false,
            onAuthorizationUrl: (url) => {
              output.write(`Open this URL to sign in with Keycloak:\n${url}\n`);
            },
          });
      output.write(`logged in as ${creds.email} @ ${api}\n`);
    } catch (err) {
      fail(err instanceof ApiError ? err.message : (err as Error).message);
    }
  });

program
  .command("logout")
  .description("Forget the cached token.")
  .action(async () => {
    const cleared = await clearCredentials();
    output.write(cleared ? "logged out\n" : "(not logged in)\n");
  });

program
  .command("whoami")
  .description("Show the currently logged-in user.")
  .action(async () => {
    const creds = await loadCredentials();
    if (!creds) fail("not logged in (run `a2amcp login`)");
    try {
      const me = await (await client()).me();
      output.write(`${me.email}  (${creds.apiUrl})\n`);
    } catch (err) {
      fail(err instanceof ApiError ? err.message : (err as Error).message);
    }
  });

// --- agents ---------------------------------------------------------------- #

program
  .command("agents")
  .description("List agents visible to your account (control-plane truth).")
  .option("--api <url>")
  .action(async (opts: { api?: string }) => {
    try {
      const rows = await (await client(opts.api)).listAgents();
      if (rows.length === 0) {
        output.write("(no agents)\n");
        return;
      }
      for (const r of rows) {
        output.write(
          `  ${r.name}  v${r.version}  [${r.status}]  ${r.url ?? "-"}\n`,
        );
      }
    } catch (err) {
      fail(err instanceof ApiError ? err.message : (err as Error).message);
    }
  });

program
  .command("search [query]")
  .description("Search agents visible to your account.")
  .option("--tag <tag...>", "Require one or more tags")
  .option("--skill <skill>", "Require a skill name")
  .option("--limit <n>", "Maximum results", "8")
  .option("--api <url>")
  .action(
    async (
      query: string | undefined,
      opts: { tag?: string[]; skill?: string; limit: string; api?: string },
    ) => {
      try {
        const rows = await searchVisibleAgents(await client(opts.api), {
          query,
          tags: opts.tag,
          skill: opts.skill,
          limit: Number.parseInt(opts.limit, 10),
        });
        if (rows.length === 0) {
          output.write("(no matching agents)\n");
          return;
        }
        const enabled = new Set((await loadConfig()).agents.map((agent) => agent.name));
        for (const row of rows) {
          const marker = enabled.has(row.name) ? "*" : " ";
          const score = row.score !== null && row.score !== undefined
            ? ` score=${row.score.toFixed(3)}`
            : "";
          const setup = row.setup_required ? " setup-required" : "";
          output.write(`${marker} ${summarizeAgent(row)}${score}${setup}\n`);
        }
        output.write("* already enabled locally\n");
      } catch (err) {
        fail(err instanceof ApiError ? err.message : (err as Error).message);
      }
    },
  );

program
  .command("list")
  .description("List the agents this gateway exposes locally.")
  .action(async () => {
    const cfg = await loadConfig();
    if (cfg.agents.length === 0) {
      output.write("(no agents added; try `a2amcp add <name>`)\n");
      return;
    }
    for (const a of cfg.agents) {
      output.write(`  ${a.name}  ${a.url}${a.mcpPath ?? "/mcp"}\n`);
    }
  });

program
  .command("add <name>")
  .description("Enable an agent in this gateway (queries control plane for URL).")
  .option("--url <url>", "Skip the control-plane lookup and use this URL")
  .option("--api <url>")
  .action(
    async (name: string, opts: { url?: string; api?: string }) => {
      try {
        const creds = await loadCredentials();
        const refreshed = creds ? await refreshCredentialsIfNeeded(creds) : null;
        const apiUrl = resolveApiUrl(opts.api ?? creds?.apiUrl);
        const added = await enableAgentByName({
          cp: opts.url ? undefined : new ControlPlaneClient(apiUrl, refreshed?.token ?? null),
          apiUrl,
          name,
          url: opts.url,
        });
        output.write(`${added.replaced ? "updated" : "added"} ${name} -> ${added.endpoint}\n`);
        output.write("restart your MCP client, or ask it to refresh tools, to pick up changes.\n");
      } catch (err) {
        fail(err instanceof ApiError ? err.message : (err as Error).message);
      }
    },
  );

program
  .command("remove <name>")
  .alias("rm")
  .description("Remove an agent from this gateway.")
  .action(async (name: string) => {
    const ok = await removeAgent(name);
    output.write(ok ? `removed ${name}\n` : `${name} was not enabled\n`);
  });

// --- doctor ---------------------------------------------------------------- #

program
  .command("doctor")
  .description("Probe each enabled agent's /mcp endpoint and report tool counts.")
  .action(async () => {
    const cfg = await loadConfig();
    const creds = await loadCredentials();
    const refreshed = creds ? await refreshCredentialsIfNeeded(creds) : null;
    if (cfg.agents.length === 0) {
      output.write("(no agents enabled)\n");
      return;
    }
    const { UpstreamAgent } = await import("./upstream.js");
    for (const a of cfg.agents) {
      const u = new UpstreamAgent({
        name: a.name,
        url: a.url,
        mcpPath: a.mcpPath,
        token: refreshed?.token ?? null,
      });
      try {
        const tools = await u.listTools();
        output.write(`  ${a.name}: ok (${tools.length} tools)\n`);
      } catch (err) {
        output.write(`  ${a.name}: FAIL  ${(err as Error).message}\n`);
      }
    }
  });

// --- print-config ---------------------------------------------------------- #

program
  .command("print-config")
  .description("Print the JSON snippet to paste into your MCP client config.")
  .action(async () => {
    const snippet = {
      mcpServers: {
        a2a: {
          command: "npx",
          args: ["-y", "a2amcp"],
        },
      },
    };
    output.write(JSON.stringify(snippet, null, 2) + "\n");
  });

program.parseAsync(process.argv).catch((err) => {
  stderr.write(`a2amcp: ${(err as Error).message}\n`);
  process.exit(1);
});
