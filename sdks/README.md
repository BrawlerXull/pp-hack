# Checkout Guard SDKs

Client libraries for the public guard API (`POST /v1/guard/check`, `POST /v1/guard/outcome`), generated with
[APIMatic](https://www.apimatic.io/) from [src/spec/openapi.json](src/spec/openapi.json): the same spec a
deployment serves at `/api/v1/openapi.json`.

| Folder | Language |
|---|---|
| [typescript/](typescript/) | Node.js / TypeScript |
| [python/](python/) | Python |
| [php/](php/) | PHP |

Get an API key on the dashboard's **Connect store** page, then follow its quickstart: the SDK call replaces
the `fetch` / `httpx` / `curl` call shown there. Each SDK sends the key as `X-Guard-Key`.

## Python example

```python
from checkoutguard.checkout_guard_client import CheckoutGuardClient
from checkoutguard.http.auth.custom_header_authentication import CustomHeaderAuthenticationCredentials
from checkoutguard.models.check_in import CheckIn
from checkoutguard.models.method_enum import MethodEnum

client = CheckoutGuardClient(
    custom_header_authentication_credentials=CustomHeaderAuthenticationCredentials(x_guard_key="cg_sk_..."),
)
guard = client.guard.check(CheckIn(sku="TEA-3", method=MethodEnum.CARD, fingerprint_hash=device_hash, amount=3.00))
if guard.decision == "block":
    ...  # don't create the PayPal order
# otherwise put guard.custom_id on the order, and guard.sca_method (if set) in its card verification
```

Each SDK's own README and `doc/` folder list every model and option.

## Regenerate

```bash
npx @apimatic/cli auth login                          # once, with a free APIMatic account
./sdks/generate.sh https://checkout-guard-web.onrender.com
```

`generate.sh` exports the spec from the backend (`backend/scripts/export_openapi.py`), validates it with
APIMatic, and writes the three SDKs.
