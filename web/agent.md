# web agent notes

## purpose

`web/` is the pnpm/turbo workspace for A2A Cloud's public and product web surfaces. It contains the authenticated dashboard, marketing site, docs, shared UI/analytics packages.

## workspace shape

- Package manager/runtime: `pnpm@10.15.0`, Node 20 in Docker images, TypeScript 5.5.
- Workspace packages: `apps/*` and `packages/*`; root scripts fan out through Turbo.
- `apps/dashboard`: Vite + React SPA for `https://app.a2acloud.io`. Uses `react-router-dom`, Vitest, Tailwind, `@a2a/analytics`, and `@a2a/design-system`.
- `apps/docs`: Next.js 15 SSR docs for `docs.a2acloud.io`. Markdown content is partly hand-written and partly generated from the `a2a_pack` SDK/CLI.
- `packages/analytics`: PostHog bootstrap plus anonymous ID and attribution helpers used by all web surfaces.
- `packages/design-system`: shared Tailwind preset, CSS helpers, and React primitives such as `Button`, `RuntimeSection`, `StatusPill`, `ReceiptLedger`.
- `packages/art`: small shared brand/art constants.
- `skills/`: local Codex skills. Use `skills/a2a-dashboard-design-system` for dashboard UI work and `skills/a2a-brand-design-system` for landing/brand/design-system work.

## common commands

Run from `web/` unless noted:

- `pnpm install`
- `pnpm typecheck` / `pnpm build` / `pnpm lint`
- `pnpm dev:dashboard` starts Vite on `5173`.
- `pnpm dev:docs` starts Next on `3001`.
- `pnpm --filter a2a-dashboard test` runs dashboard Vitest tests.
- `pnpm --filter a2a-dashboard smoke:kernel` checks built dashboard kernel wiring and optionally `A2A_DASHBOARD_SMOKE_URL`.
- `pnpm app-flow:build`, `pnpm app-flow:prod`, `pnpm app-flow:next`, `pnpm app-flow:deploy-plan`, and `pnpm app-flow:loop:*` wrap `@a2a/app-flow-tests`.

Docker images build from the `web/` root:

- `docker build -f apps/dashboard/Dockerfile .`
- `docker build -f apps/docs/Dockerfile .`

## integration points

- Dashboard dev server proxies `/v1` and `/healthz` to `A2A_DASHBOARD_PROXY_TARGET`, defaulting to `http://api.127-0-0-1.nip.io`.
- Dashboard production is nginx. It serves the SPA, proxies `/v1/*` to `control-plane.control-plane.svc.cluster.local`, disables buffering for SSE/streaming, and writes `/analytics-config.js` at container startup.
- Browser auth uses the host-only, httpOnly `__Host-a2a_session` cookie. Dashboard calls `/v1/auth/session` through its same-origin proxy; sibling subdomains do not receive the session.
- Landing uses `CONTROL_PLANE_URL` for public agents, bounties, proofs, receipts, and replay data. ISR cache tags are revalidated through `/api/revalidate` when `REVALIDATE_SECRET` is configured.
- Blog uses `BLOG_DATABASE_URL` or `DATABASE_URL`, `BLOG_API_KEY`, `NEXT_PUBLIC_A2A_APP_URL`, and `A2A_AUTH_BASE_URL`.
- Analytics is optional. If `POSTHOG_KEY` is absent, generated config disables analytics. `POSTHOG_API_URL` defaults to `https://e.a2acloud.io` (first-party reverse proxy to PostHog Cloud US); script URL defaults to `<api url>/static/array.js`.
- Docs generation uses `apps/docs/scripts/gen.py`. Local dev can run `python scripts/gen.py`; Docker installs `a2a-pack` in a Python generation stage before `next build`.

## testing and deployment

- Root Turbo tasks depend on upstream package builds; app builds generally include `tsc --noEmit` or `tsc -b`.
- Dashboard has many targeted unit/component tests under `apps/dashboard/src/**/*.test.*`; prefer focused Vitest runs for dashboard changes, then the full dashboard test/typecheck when behavior or shared helpers change.
- CI is `.github/workflows/ci.yml` (typecheck + tests). Each app has a `Dockerfile`; image publishing and Kubernetes manifests live in the operator's deployment repo, not here.
- Reference domains: `app.<domain>` dashboard, `docs.<domain>` docs.

## cautions

- Do not assume npm commands from app READMEs are canonical here; this workspace is pnpm-based.
- The dashboard should stay dense and operational. Use existing local patterns, `Icon`, Tailwind tokens, and the dashboard design skill before introducing broad UI abstractions.
- Brand work should use `@a2a/design-system` primitives and the brand skill; avoid inventing a new visual system.
- Docs reference/language markdown may be regenerated. Avoid hand-editing generated reference files unless that is explicitly the task.
