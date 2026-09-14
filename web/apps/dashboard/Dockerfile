# Build from the monorepo root:
# docker build -f apps/dashboard/Dockerfile .
FROM node:20-slim AS build
WORKDIR /repo
RUN corepack enable

COPY package.json pnpm-lock.yaml pnpm-workspace.yaml turbo.json ./
COPY packages/analytics/package.json packages/analytics/package.json
COPY packages/design-system/package.json packages/design-system/package.json
COPY apps/dashboard/package.json apps/dashboard/package.json
RUN pnpm install --filter a2a-dashboard... --frozen-lockfile

COPY packages/analytics packages/analytics
COPY packages/design-system packages/design-system
COPY apps/dashboard apps/dashboard
RUN pnpm --filter a2a-dashboard build

FROM nginx:1.27-alpine
COPY --from=build /repo/apps/dashboard/dist /usr/share/nginx/html
COPY apps/dashboard/nginx.conf /etc/nginx/conf.d/default.conf
COPY apps/dashboard/docker-entrypoint.d/40-analytics-config.sh /docker-entrypoint.d/40-analytics-config.sh
RUN find /usr/share/nginx/html -type f \
    \( -name '*.js' -o -name '*.css' -o -name '*.json' -o -name '*.html' -o -name '*.svg' \) \
    -exec gzip -k -9 {} \; \
  && chmod +x /docker-entrypoint.d/40-analytics-config.sh
EXPOSE 80
