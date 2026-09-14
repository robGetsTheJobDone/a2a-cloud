# Server-rendered packed frontends

Status: design started

This document defines the target model for deploying server-rendered frontends
with an A2A Pack agent. The first supported framework should be Next.js
standalone output, but the runtime contract should stay framework-neutral.

## Goals

- Let an agent ship a server-rendered web app instead of only a static bundle.
- Keep one public agent hostname for browser UI and A2A/API traffic.
- Enforce `frontend.auth` before a browser request reaches the frontend server.
- Keep `/_a2a/*` owned by the agent runtime when the frontend is mounted at `/`.
- Work for Python and sidecar agents.
- Work with both on-demand Knative services and always-on Kubernetes
  deployments.

## Non-goals

- Running arbitrary long-lived background daemons from `a2a.yaml`.
- Exposing a second public frontend service in the first implementation.
- Making Next.js auth-aware by default. The platform boundary remains the agent
  process or sidecar proxy.

## Manifest shape

Static packed frontends keep the existing shape:

```yaml
frontend:
  type: static-spa
  path: frontend
  build: npm run build
  dist: dist
  mount: /
  auth: inherit
```

Server-rendered frontends add a distinct type and a start command:

```yaml
frontend:
  type: server-rendered
  framework: nextjs
  path: frontend
  build: npm run build
  start: npm run start
  port: 3000
  mount: /
  auth: inherit
```

Field semantics:

- `type`: `server-rendered` selects the SSR/runtime-proxy path.
- `framework`: optional hint for scaffold/template validation. Start with
  `nextjs`.
- `path`: frontend source directory.
- `build`: command run in the Node build stage.
- `start`: command run at container start for the frontend server.
- `port`: loopback port the frontend server listens on. Default `3000`.
- `mount`: public mount path. Default `/`.
- `auth`: same values as static packed frontends: `inherit`, `platform`,
  `public`.

Do not use `dist` for server-rendered frontends. `dist` is static-bundle
specific and should remain required only for `static`, `static-spa`, and `spa`.

## Runtime model

Use a same-pod, same-container process model for the first implementation:

```text
browser
  |
  v
agent public hostname
  |
  v
agent runtime / sidecar on :8000
  |-- /_a2a/*, /invoke, /mcp, /.well-known/* -> agent runtime
  |-- frontend browser paths -> auth gate -> proxy to 127.0.0.1:3000
                                      |
                                      v
                              Next.js standalone server
```

The agent runtime remains the public ingress target. The frontend server binds
only to loopback. That keeps auth, session, and route ownership centralized in
A2A Pack.

For `mount: /`, the runtime must reserve `/_a2a/*` for agent APIs and proxy all
other browser paths to the frontend server. For `mount: /app`, only `/app/*`
is proxied.

## Auth boundary

`frontend.auth` is enforced before proxying:

- `public`: proxy without a platform session.
- `platform`: require a valid platform browser session.
- `inherit`: require a platform session when the agent is not public.

The frontend server may still call `/auth/session` for UI state, but it must not
be the only auth boundary. A missing or invalid browser session must fail in the
agent runtime before the request reaches Next.js.

HTML requests with `A2A_LOGIN_URL` should redirect to the dashboard login with a
`next` parameter. Non-HTML requests should return `401` JSON, matching static
frontend behavior.

## Docker/scaffold model

The control-plane scaffold should generate a Node build stage:

```dockerfile
FROM node:20-bookworm-slim AS frontend-build
WORKDIR /frontend
COPY frontend/ ./
RUN if [ -f package-lock.json ]; then npm ci; \
    elif [ -f yarn.lock ]; then corepack enable && yarn install --frozen-lockfile; \
    elif [ -f pnpm-lock.yaml ]; then corepack enable && pnpm install --frozen-lockfile; \
    else npm install; fi
RUN npm run build
```

For Next.js standalone, the runtime image should copy:

```dockerfile
COPY --from=frontend-build /frontend/.next/standalone /app/.a2a/frontend-server
COPY --from=frontend-build /frontend/.next/static /app/.a2a/frontend-server/.next/static
COPY --from=frontend-build /frontend/public /app/.a2a/frontend-server/public
```

Runtime env:

```dockerfile
ENV A2A_FRONTEND_KIND=server-rendered
ENV A2A_FRONTEND_MOUNT=/
ENV A2A_FRONTEND_AUTH=inherit
ENV A2A_FRONTEND_PROXY_URL=http://127.0.0.1:3000
ENV A2A_FRONTEND_START="npm run start"
ENV A2A_FRONTEND_WORKDIR=/app/.a2a/frontend-server
```

The initial implementation can use an entrypoint wrapper that starts the
frontend process and the agent process, forwards signals, and exits non-zero if
either required process exits unexpectedly.

## Next.js template

The existing Next.js scaffold is a static export template:

```js
const nextConfig = {
  output: "export",
  trailingSlash: true,
};
```

The SSR template should switch to standalone output:

```js
const nextConfig = {
  output: "standalone",
};
```

The generated `package.json` should include:

```json
{
  "scripts": {
    "dev": "next dev",
    "build": "next build",
    "start": "next start -H 127.0.0.1 -p ${PORT:-3000}"
  }
}
```

If the final runtime uses `.next/standalone/server.js` directly, the scaffold
can replace `start` with:

```json
{
  "start": "HOSTNAME=127.0.0.1 PORT=${PORT:-3000} node server.js"
}
```

## CLI behavior

`a2a frontend build` should:

- run `frontend.build`;
- for static frontends, require `dist/index.html`;
- for server-rendered frontends, require the framework-specific server output
  (`.next/standalone/server.js` for Next.js).

`a2a frontend info` should report:

- `type`;
- `framework`;
- `mount`;
- `auth`;
- `build`;
- `start`;
- `port`;
- static `ready` or SSR `ready`.

Local dev should not require the agent process to supervise Next.js at first.
The recommended local flow can remain two terminals:

```bash
a2a dev
cd frontend && npm run dev
```

The production proxy/process-supervision path is the required hosted behavior.

## Control-plane validation

Control-plane scaffold should reject invalid SSR configs before Docker build:

- unsupported `frontend.type`;
- `server-rendered` without `build` or `start`;
- unsafe `path`;
- invalid `port`;
- `dist` on `server-rendered` unless ignored with a warning;
- unsupported `framework` values until implemented.

For static frontends without `build`, it should continue to require
`dist/index.html` before stamping Dockerfiles.

## Rollout plan

1. Extend shared frontend config parsing with a typed server-rendered config.
2. Add A2A Pack template support for `--frontend nextjs --frontend-mode server-rendered`
   or a clearer CLI flag before exposing it broadly.
3. Add Dockerfile/runtime env generation for SSR in both A2A Pack local
   Dockerfile generation and control-plane scaffold.
4. Add process supervision and reverse proxy support in Python runtime and
   sidecar runtime.
5. Add docs for static versus server-rendered frontends.
6. Add deployment tests that prove `frontend.auth: platform` blocks SSR HTML and
   asset requests before proxying.

