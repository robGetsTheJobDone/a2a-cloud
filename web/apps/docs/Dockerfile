# Build from the repository's web/ directory:
# docker build -f apps/docs/Dockerfile .
#
# Reference Markdown is generated and checked in from the matching monorepo
# SDK revision. CI runs scripts/check.py before this build and rejects drift.

FROM node:20-alpine AS deps
WORKDIR /repo
RUN corepack enable

COPY package.json pnpm-lock.yaml pnpm-workspace.yaml turbo.json ./
COPY packages/analytics/package.json packages/analytics/package.json
COPY packages/design-system/package.json packages/design-system/package.json
COPY apps/docs/package.json apps/docs/package.json
RUN pnpm install --filter a2a-docs... --frozen-lockfile

FROM node:20-alpine AS builder
WORKDIR /repo
RUN corepack enable
COPY --from=deps /repo/node_modules ./node_modules
COPY --from=deps /repo/apps/docs/node_modules ./apps/docs/node_modules
COPY --from=deps /repo/packages/analytics/node_modules ./packages/analytics/node_modules
COPY --from=deps /repo/packages/design-system/node_modules ./packages/design-system/node_modules
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml turbo.json ./
COPY packages/analytics packages/analytics
COPY packages/design-system packages/design-system
COPY apps/docs apps/docs
ENV NEXT_TELEMETRY_DISABLED=1
RUN pnpm --filter a2a-docs build

FROM node:20-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1
RUN addgroup -S nodejs && adduser -S next -G nodejs
COPY --from=builder /repo/apps/docs/public ./apps/docs/public
COPY --from=builder --chown=next:nodejs /repo/apps/docs/.next/standalone ./
COPY --from=builder --chown=next:nodejs /repo/apps/docs/.next/static ./apps/docs/.next/static
USER next
EXPOSE 3001
ENV PORT=3001
CMD ["node", "apps/docs/server.js"]
