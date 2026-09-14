# agent-reviewer

Pre-deploy audit agent. Reads another agent's source from Gitea, applies
three skill bundles (security, A2A Pack best practices, grant scope), and
returns a typed `ReviewReport`.

## Skill: `review`

```
review(agent_name: str, ref: str = "main", owner: str | None = None, mode: str = "audit") -> ReviewReport
```

Workflow:

1. Mint a short-lived **read-only** Gitea token via the control plane.
2. Spin up a DeepAgents graph backed by a `GiteaBackend` pointed at
   `{owner}/{agent_name}@{ref}`.
3. Read the source, consult the bundled skills, run sandbox checks
   (`a2a card`, `ruff`), produce findings.
4. Release the Gitea token in `finally`.

`mode="improvements"` keeps the same read-only checks and additionally asks
for at most three source-specific robustness, test, tool-usability, or
operator-ergonomics ideas. Agent Studio uses that mode for its daily review.

## Output

`ReviewReport`:

```python
class Finding(BaseModel):
    severity: "critical" | "warning" | "info"
    category: "security" | "correctness" | "ergonomics" | "policy" | "scope"
    message: str
    file: str | None
    line: int | None
    suggestion: str | None

class ReviewReport(BaseModel):
    ok: bool                # True iff zero critical findings
    agent_name: str
    ref: str
    summary: str
    findings: list[Finding]
```

## Operational notes

- Read-only by design. Never writes to the target repo, never deploys.
- Uses the `meta-agent-reader` Gitea service user via the platform's
  `POST /v1/platform/gitea-token` endpoint.
- Caller must run through the orchestrator (cp_jwt is forwarded).
