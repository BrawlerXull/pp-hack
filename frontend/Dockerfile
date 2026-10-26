# Checkout Guard frontend: Next.js (UI only). /api/* is proxied to BACKEND_URL, which is compiled into the
# build's rewrites, so it must be known at build time (compose and Render pass it as a build arg).
FROM node:22-slim AS build
WORKDIR /app
ENV COREPACK_ENABLE_DOWNLOAD_PROMPT=0
RUN corepack enable
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile
COPY . .
# No default: a build without it would silently proxy /api to a host that does not exist there, and the
# site would look healthy while every API call fails.
ARG BACKEND_URL
ENV BACKEND_URL=$BACKEND_URL NEXT_TELEMETRY_DISABLED=1
RUN test -n "$BACKEND_URL" || { echo "BACKEND_URL build arg is required (the API's URL or private host)" >&2; exit 1; }
RUN pnpm build

FROM node:22-slim
WORKDIR /app
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1
ARG BACKEND_URL
ENV BACKEND_URL=$BACKEND_URL
COPY --from=build --chown=node /app ./
USER node
EXPOSE 3000
# PORT is set by Render; 3000 locally.
# next directly, not `pnpm start`: no package-manager download when the container starts.
CMD ["sh", "-c", "exec node_modules/.bin/next start -H 0.0.0.0 -p ${PORT:-3000}"]
