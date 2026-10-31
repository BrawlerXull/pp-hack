"""Write the public guard API's OpenAPI spec (the one /v1/openapi.json serves) to sdks/src/spec/openapi.json,
the input APIMatic generates the SDKs from (sdks/generate.sh).

  uv run python scripts/export_openapi.py [https://checkout-guard-web.onrender.com]

The URL is the deployed dashboard; the spec's server is its /api proxy. Default: PUBLIC_APP_URL.
"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

from app.api.integration import public_spec
from app.config import get_settings
from app.main import app

OUT = Path(__file__).resolve().parents[2] / "sdks" / "src" / "spec" / "openapi.json"

if __name__ == "__main__":
    if len(sys.argv) > 1:
        get_settings().public_app_url = sys.argv[1]
    spec = public_spec(SimpleNamespace(app=app))  # type: ignore[arg-type]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(spec, indent=2) + "\n")
    print(f"wrote {OUT} (server {spec['servers'][0]['url']})")
