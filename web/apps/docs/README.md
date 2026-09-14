# docs

Documentation site at `docs.a2acloud.io`. Next.js 15 SSR, same dark
theme as the dashboard + landing.

Content combines task-based platform guides with generated SDK, CLI, and
control-plane references. Generated files are checked into the same monorepo
revision as their source and verified by `scripts/check.py`; the production
image never downloads an unpinned SDK package.

## LLM-friendly endpoints

- `/llms.txt` — [llmstxt.org](https://llmstxt.org) index with a link to every page
- `/llms-full.txt` — every page concatenated into one plain-text blob
- `/raw/<slug>` or `/raw/<slug>.md` — plain Markdown for any page

## Dev

```bash
python web/apps/docs/scripts/gen.py
python web/apps/docs/scripts/gen_control_plane_api.py
python web/apps/docs/scripts/check.py --repo-root .
cd web
pnpm install
pnpm --filter a2a-docs dev        # http://localhost:3001
```

## Deploy

`git push` → Gitea Actions verifies generated reference drift and links, builds
the docs snapshot from that exact commit, and pushes the image. ArgoCD
reconciles ingress on `docs.a2acloud.io` with cert-manager TLS.

## Routes

- `/`                — overview
- `/quickstart`      — install + scaffold + deploy
- `/platform/...`    — dashboard workflows, runtime, governance, and API
- `/concepts/...`    — agents, grants, sandbox, marketplace
- `/languages/...`   — auto-generated per-language SDK guides
- `/reference/...`   — auto-generated from `a2a_pack` source
- `/llms.txt`        — index
- `/llms-full.txt`   — full corpus
- `/raw/<slug>`      — plain Markdown source for any page (`.md` optional)
