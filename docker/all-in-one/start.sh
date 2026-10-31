#!/usr/bin/env bash
# Migrate, then run the API (with the worker inside) and the Next.js server. If either stops, the container
# stops, and the platform restarts it.
set -euo pipefail
cd /app
alembic upgrade head
# --no-proxy-headers: the app reads X-Forwarded-For itself, counting TRUSTED_PROXY_HOPS from the right.
uvicorn app.main:app --host 127.0.0.1 --port 8000 --no-proxy-headers &
cd /web
node node_modules/next/dist/bin/next start -H 0.0.0.0 -p "${PORT:-10000}" &
wait -n
exit 1
