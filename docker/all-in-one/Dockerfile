# Checkout Guard in ONE container, for a single free web service (Render's free tier has no background
# workers and gives 750 instance hours a month: enough for one always-on service, not three).
# Next.js serves the UI on $PORT and proxies /api/* to the API on 127.0.0.1:8000, which also runs the
# worker in-process (RUN_WORKER_IN_API=true). Build context: the repository root.
# The paid layout (separate API, worker and frontend) stays in render.yaml and the per-app Dockerfiles.

FROM node:22-slim AS web
WORKDIR /web
ENV COREPACK_ENABLE_DOWNLOAD_PROMPT=0 NEXT_TELEMETRY_DISABLED=1 BACKEND_URL=http://127.0.0.1:8000
RUN corepack enable
COPY frontend/package.json frontend/pnpm-lock.yaml frontend/pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile
COPY frontend/ ./
# Optional AG Studio licence (compiled into the browser bundle); without it the Investigate page shows a
# trial watermark.
ARG NEXT_PUBLIC_AG_STUDIO_LICENSE_KEY
ENV NEXT_PUBLIC_AG_STUDIO_LICENSE_KEY=$NEXT_PUBLIC_AG_STUDIO_LICENSE_KEY
RUN pnpm build

FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.13.0 /uv /uvx /usr/local/bin/
COPY --from=web /usr/local/bin/node /usr/local/bin/node
WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_NO_SYNC=1 \
    PATH="/app/.venv/bin:$PATH" \
    NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    BACKEND_URL=http://127.0.0.1:8000 \
    RUN_WORKER_IN_API=true \
    NODE_OPTIONS=--max-old-space-size=192
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY backend/ ./
RUN uv sync --frozen --no-dev
COPY --from=web /web /web
COPY docker/all-in-one/start.sh /start.sh

RUN useradd --create-home --uid 10001 guard && chown -R guard /app /web
USER guard
EXPOSE 10000
CMD ["bash", "/start.sh"]
