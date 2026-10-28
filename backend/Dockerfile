# Checkout Guard backend: one image for the API (default command) and the worker
# (command: python -m app.worker.main). Migrations: uv run alembic upgrade head.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.0 /uv /uvx /usr/local/bin/

WORKDIR /app
# UV_NO_SYNC: `uv run` uses the venv built here as is (no dev tools, no network at start-up).
ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_NO_SYNC=1 \
    PATH="/app/.venv/bin:$PATH"

# Dependencies first, so a code change does not reinstall them.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY . .
RUN uv sync --frozen --no-dev

RUN useradd --create-home --uid 10001 guard && chown -R guard /app
USER guard

EXPOSE 8000
# PORT is set by Render; 8000 locally. --proxy-headers: the shop reads the client IP behind Render's proxy.
# --no-proxy-headers: uvicorn must not replace the client address with X-Forwarded-For (with '*' anyone
# could set it); the app reads that header itself, counting TRUSTED_PROXY_HOPS from the right.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --no-proxy-headers"]
