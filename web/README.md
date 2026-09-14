# a2a web

Monorepo for the public web surfaces and shared TypeScript packages.

## Layout

- `apps/dashboard` - Vite dashboard app
- `apps/docs` - Next.js docs
- `packages/analytics` - shared PostHog bootstrap, anonymous ID, and first/latest touch attribution
- `packages/art` - shared brand/art constants and future asset generation

## Commands

```sh
pnpm install
pnpm typecheck
pnpm --filter a2a-dashboard build
pnpm --filter a2a-docs build
```

Docker images build from the monorepo root:

```sh
docker build -f apps/dashboard/Dockerfile .
docker build -f apps/docs/Dockerfile .
```

The docs image expects an `a2a/` SDK checkout at the repo root before Docker build; the Gitea workflow prepares that checkout.
