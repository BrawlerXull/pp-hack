
# Check In

One checkout, as the shop's server sees it, before the PayPal order is created.

## Structure

`CheckIn`

## Fields

| Name | Type | Tags | Description |
|  --- | --- | --- | --- |
| `sku` | `str` | Required | The item being bought (your product code)<br><br>**Constraints**: *Maximum Length*: `64` |
| `method` | [`MethodEnum`](../../doc/models/method-enum.md) | Required | card: card fields; wallet: the PayPal button |
| `fingerprint_hash` | `str` | Required | A stable hash of the buyer's device (e.g. SHA-256 of IP + user agent), never the raw values<br><br>**Constraints**: *Minimum Length*: `8`, *Maximum Length*: `64` |
| `challenge_token` | str \| None | Optional | This is a container for any-of cases. |
| `amount` | float \| str \| None | Optional | This is a container for any-of cases. |
| `email_domain` | str \| None | Optional | This is a container for any-of cases. |
| `client_ip` | str \| None | Optional | This is a container for any-of cases. |
| `dry_run` | `bool` | Optional | Answer as usual but record nothing: for testing an integration. attempt_id and custom_id are null.<br><br>**Default**: `False` |

## Example

```python
from checkoutguard.models.check_in import CheckIn
from checkoutguard.models.method_enum import MethodEnum

check_in = CheckIn(
    sku='sku6',
    method=MethodEnum.CARD,
    fingerprint_hash='fingerprint_hash4',
    challenge_token='String7',
    amount=135.46,
    email_domain='String1',
    client_ip='String1',
    dry_run=False
)
```

