# agent-studio

First-party coordinator for building, reviewing, and improving managed A2A
agents.

`agent-studio` is intentionally narrower than the synthetic/composable
meta-agent roadmap. It coordinates the already shipped deployed-agent rails:

- `agent-builder.build` for the first private version
- `agent-reviewer.review` for structured critique
- `code-editor-agent.turn` for bounded improvement patches
- control-plane helper endpoints for card refresh, deployment status, and
  code-editor opt-in

Current status:

- P1 provides the stable agent surface and typed coordinator records.
- P2 adds a redacting control-plane helper client for opt-in, card refresh,
  live state, and deployment status.
- P3 starts the real bootstrap path by calling `agent-builder.build` through
  `ctx.call`.
- P4 evaluates live skill schemas with deterministic bounded smoke calls.
- P5 wires `agent-reviewer.review` and `code-editor-agent.turn` into the
  bounded improve loop with `push_on_failure=False`.
- P6 checks live head freshness after pushed edits and returns a structured
  private-by-default final report for success or partial failure.
- `upgrade_agent` applies one explicitly approved daily-review idea to an
  existing agent. It refuses stale source, runs Code Editor with
  `push_on_failure=False`, waits for the exact deployment, and reviews the
  upgraded revision before reporting success.

## Local checks

```bash
cd apps/agent-studio
PYTHONPATH=../a2a python -m pytest
PYTHONPATH=../a2a a2a card --project .
```
