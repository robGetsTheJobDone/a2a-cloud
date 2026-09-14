# Procurement Agent

This A2A Pack example is the first procurement-system slice:

- upload supplier quote PDFs or text exports
- extract supplier, part, price, delivery, and terms
- persist company procurement memory in an org-scoped database
- compare historical quotes
- create purchase requests
- approve, reject, or request more quotes
- record supplier outcomes
- produce supplier scorecards and owner KPIs

Requires: a local SQLite file (the default) or a Postgres URL, plus Node for
the frontend build. No LLM key and no network beyond `npm install`.

Run locally from `sdk/a2a-pack`:

```bash
cd examples/procurement_agent/frontend
npm install
npm run build

cd ../../..
PROCUREMENT_DATABASE_URL=sqlite:///procurement.sqlite3 a2a dev --local --project examples/procurement_agent
```

`a2a dev` without `--local` syncs to a cloud dev box and needs `a2a login`.
The agent also runs without the frontend build — `a2a dev --local` reports the
missing `dist` and still serves the tools, card, and `/_dev` console.

Regenerate the React client's typed OpenAPI bindings after changing the agent's tools:

```bash
a2a openapi client --project examples/procurement_agent \
  --out frontend/src/agent-client \
  --spec-out frontend/src/agent-client/openapi.json \
  --skill ingest_quote_payloads \
  --skill compare_quotes \
  --skill create_purchase_request \
  --skill decide_purchase_request \
  --skill supplier_scorecard \
  --skill executive_dashboard
```

Deployment uses the `resources.databases` declaration in `a2a.yaml` and the
Postgres migration under `db/migrations`.
