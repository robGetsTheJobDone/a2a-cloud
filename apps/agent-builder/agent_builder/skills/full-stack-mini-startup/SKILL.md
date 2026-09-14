---
name: full-stack-mini-startup
description: Build a useful one-page A2A product with a packed React frontend, typed backend tools, platform auth, managed Postgres, browser-safe uploads, deterministic fixtures, and reload-safe persistence. Use for application contracts with profile=full_stack or requests for a UI plus durable state.
---
# Full-Stack Mini Startup

Build a product, not a generic skill runner or chat card. The deployed agent is
the backend and serves its frontend from the same origin at `/app`.

## Required sequence

1. Inspect the managed workspace before scaffolding. If `a2a.yaml`, `agent.py`,
   and the requested product files already exist, preserve that implementation
   and make the smallest evidence-driven repair. Do not call
   `init_agent_template` or rewrite a working existing app merely because a
   validation/retry run supplied the bootstrap version again.
2. Only for a genuinely new or incomplete project, call
   `init_agent_template(name, description, frontend="react",
   profile="full_stack")`, then read the generated manifest, Python source,
   frontend, migration, and `tests/test_full_stack_contract.py`.
3. Replace the generic React skill runner with the requested single workflow.
4. Implement small typed backend tools named exactly as required by the
   Application contract. Keep deterministic policy/calculation outside an LLM.
5. Customize the database schema and add product tests for the supplied success,
   failure, and reload fixtures.
6. Run `test_agent_in_sandbox`. Do not deploy while its exit code is nonzero.
7. Deploy privately with `cp_deploy_tarball` only after the complete project
   passes.

## Auth and tenant boundary

Use `PlatformUserAuth`, not `NoAuth`, for stateful product apps. Derive a stable
tenant key from the authenticated caller. Default to user isolation:

```python
def tenant_key(ctx: RunContext[PlatformUserAuth]) -> str:
    auth = ctx.auth
    stable_id = auth.user_id or auth.sub
    if not stable_id:
        raise PermissionError("stable platform identity required")
    return f"user:{stable_id}"
```

Use `org:{org_slug}` only when the Application contract explicitly chooses org
scope. Include `tenant_key` in every table key/query; never accept it from a
browser argument. Parameterize SQL and never return `DATABASE_URL` or database
exceptions to the client.

Mirror the manifest resource in Python so the live Card is truthful:

```python
platform_resources = AgentPlatformResources(
    databases=(
        AgentDatabase(
            name="<manifest database name>",
            scope="user",
            access_mode="read_write",
            env=AgentDatabaseEnv(url="DATABASE_URL"),
            migrations=AgentDatabaseMigrations(path="db/migrations"),
        ),
    )
)
```

Open short-lived psycopg connections from `os.environ["DATABASE_URL"]`. Put
`statement_timeout`, `lock_timeout`, and `idle_in_transaction_session_timeout`
in psycopg's connection `options` string. PostgreSQL `SET` and `SET LOCAL`
statements do not accept `%s` bind parameters; never emit parameterized
`SET`, `SET LOCAL statement_timeout = %s`, or equivalent SQL. Keep all related
record and receipt writes in one connection transaction so either all commit or
all roll back. Make schema migrations idempotent. Store typed columns for
values users filter/sort, and JSONB only for bounded supporting detail. A
create/analyze tool must commit before returning. A separate list/read tool
must prove a later invocation can reopen the record.

## Frontend contract

Use the scaffolded `frontend/src/a2a.js`; it resolves `/app/config.json`,
requires the inherited session, and calls the same-origin backend. Never
hard-code an agent URL. Keep its `unwrapInvokeResponse(payload)` behavior:
`POST /invoke/<skill>` returns `{result, events, artifacts, grant_id}`, while
the product component must receive the declared tool result inside `result`.
Do not bind the raw HTTP envelope to product state. The page must include:

- one clear input workflow rather than a general tool picker;
- loading, validation, backend error, empty, and success states;
- a durable-history/reopen view when persistence is required;
- accessible labels, keyboard controls, and useful mobile layout;
- rendered backend evidence and rationale, not mock/example results.

The page must expose stable browser-proof hooks. Use
`data-testid="agent-app"` on the product root, `agent-input` (or the exact
field-specific selector from the declared journey) on primary inputs,
`agent-submit` on the real backend action, `agent-result` on rendered success,
and `agent-download` on the real file download control. These are production
contract hooks, not a mocked test UI. When there are no explicit fill steps,
prefill one bounded deterministic success fixture so the journey can submit.

When a tool returns generated files or browser-safe media/content, render or
preview the output and provide an explicit download control using the returned
filename and media type. “Copy” is useful but does not replace “Download”. For
inline text/SVG/bytes, create a `Blob`, trigger a filename-bearing anchor, and
revoke the object URL after the click. For platform artifact URLs, use the
returned public download URL rather than exposing internal storage paths.

Do not include platform tokens, provider keys, database URLs, MinIO/LiteLLM
hosts, `.svc.cluster.local`, or internal error details in frontend code or
returned payloads.

## Browser uploads

External Agent API clients should retain a typed `FileUpload` / `UploadedFile`
tool. The packed browser currently calls JSON endpoints, so also expose a
browser bridge accepting bounded base64 payload objects:

```python
class BrowserDocument(BaseModel):
    filename: str
    media_type: str
    data_base64: str
```

Validate maximum item count, decoded byte length, media type, and filename.
Reject invalid base64 with an actionable structured validation result. Do not
trust extensions. The React page reads `File` objects, rejects them client-side
at the same limits, base64-encodes them, and calls the browser bridge. Both
upload paths must feed the same parsing/business function.

## Deterministic acceptance

Implement the exact calls in `app_spec.acceptance_calls`. Their public argument
schemas and results are part of the product contract. Return every nested field
listed in each call's `expected` object with the exact deterministic value:

- `success`: creates or analyzes the deterministic fixture and returns stable
  identifiers plus structured output. Repeating the fixture must be safe. Mark
  the tool `idempotent=True` only when every durable side effect, including
  receipts, is upserted under a stable key; otherwise declare it non-idempotent;
- `reload`: runs in a later invocation and reads the state created above;
- `failure`: rejects the specified bad input without inventing data or leaking
  internals;
- `mcp`: is safe to repeat/read and proves standard MCP `tools/call`.

Tests must cover all four behaviors when present. For date extraction, mark
ambiguous/missing dates instead of guessing. For calculations, assert exact
totals/scores. For CSV/document parsing, include malformed fixtures. For URL
fetching, reject loopback, private, link-local, metadata, non-HTTP(S), and DNS or
redirect rebinding targets; bound redirects, bytes, and time.

The final sandbox pass must build the packed frontend, run pytest, load the
Card, and confirm the Card declares the managed database. A live Card without a
working `/app` and reopen path is not success.

## Mini-startup recipes

Treat `launch_plan.recipe` as a scaffold choice while keeping the product
specific:

- `csv_tool`: bounded upload/paste, parse/validate, table preview, corrected CSV
  download, and malformed-input fixture;
- `document_generator`: structured form, document preview, first-class emitted
  artifact, and correctly named download;
- `email_assistant`: inbox/message input, policy-aware draft, approval boundary,
  and missing-setup behavior;
- `scheduled_monitor`: source/threshold form, current status, durable history,
  explicit schedule declaration, and alert failure behavior;
- `calculator`: typed inputs, deterministic calculation, rationale, and exact
  fixture assertions;
- `approval_workflow`: propose/preview separated from execute, visible approval
  state, durable audit record, and safe rejection;
- `dashboard`: durable records, useful filters/table/chart, empty/loading/error
  states, and reload proof;
- `custom`: the smallest coherent one-page workflow satisfying the capability
  and output contracts.

If `account_trial_calls` is non-zero, declare `AccountAccess(required=True,
platform_skill_calls=n, after_trial="byok")` on the agent. The platform owns
account gating and usage counts; do not reproduce them in application SQL.
