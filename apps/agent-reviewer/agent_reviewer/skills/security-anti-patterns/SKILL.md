---
name: security-anti-patterns
description: Common security mistakes in A2A agent source. Use when reviewing agent.py, a2a.yaml, or any imported module for credential leakage, unsafe code execution, network egress, or sandbox bypass.
---
# Security Anti-Patterns in A2A Agents

Review the agent's source for these high-confidence red flags. Each finding
must cite a file path and (when available) a line number. Severity guidance
is below — be strict about `critical`, conservative about `info`.

## Credential leakage (critical)

* Hard-coded API keys, tokens, passwords. Even in comments. Patterns to grep:
  `sk-`, `gho_`, `AKIA`, `xoxb-`, `Bearer `, `password=`, `api_key="..."`.
* Reading provider keys directly: `os.environ["OPENAI_API_KEY"]`,
  `A2A_LITELLM_KEY`, master keys. The agent **must** use `ctx.llm` instead.
* Echoing secrets into logs, `emit_progress`, error messages, or returned dicts.
* Writing secrets to the workspace as files.

## Unsafe execution (critical)

* `eval(...)`, `exec(...)`, `compile(...)` on caller-supplied content.
* `subprocess.run`/`Popen` with `shell=True` and a string built from caller input.
* `os.system(...)` with any caller input.
* `pickle.load`/`pickle.loads` from caller-supplied bytes.
* Running caller-supplied code *outside* `ctx.sandbox`. Any subprocess spawned
  in the agent container is unsandboxed.

## Sandbox bypass (critical)

* Calling `asyncio.create_subprocess_exec` for work that should produce
  durable files — those run in the agent container, not the sandbox. Use
  `ctx.workspace_shell` or `ctx.workspace_python`.
* Writing files outside the agent's declared `grant_write_prefixes`. The
  sandbox enforces this at the FUSE layer, but unscoped writes from the
  agent container itself are still a smell.

## Network egress (warning, critical if combined with credential exfil)

* `httpx`/`requests`/`urllib` calls to arbitrary caller-supplied URLs without
  declaring egress policy. Check `runtime = AgentRuntime(egress=...)`.
* Webhooks or callbacks that send caller data to a third party not declared
  in the agent card.

## Resource shape mismatches (warning)

* Long-running shell commands without `timeout` set.
* Unbounded loops over caller-supplied collections.
* Reading large files entirely into memory when streaming would do.

## Severity rubric

* `critical` — exploitable today, or directly violates the platform contract.
* `warning` — would fail a careful review; ship-blocker depending on context.
* `info` — style or hardening suggestion; does not block deploy.

When in doubt, prefer `warning` over `critical`. Reviewers that cry wolf get
ignored. Reviewers that miss credential leakage get fired.
