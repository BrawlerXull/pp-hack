#!/usr/bin/env bash
# Regenerate the Checkout Guard SDKs with APIMatic from the public OpenAPI spec.
#   npx @apimatic/cli auth login          # once (free APIMatic account)
#   ./sdks/generate.sh [https://your-deployed-dashboard]
set -euo pipefail
cd "$(dirname "$0")"
(cd ../backend && uv run python scripts/export_openapi.py ${1:+"$1"})
npx @apimatic/cli api validate --file=src/spec/openapi.json
for lang in typescript python php; do
  npx @apimatic/cli sdk generate --language="$lang" --input=. --destination=. --force
done
