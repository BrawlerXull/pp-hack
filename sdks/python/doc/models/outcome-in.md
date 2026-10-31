
# Outcome In

What happened to a checked order.

## Structure

`OutcomeIn`

## Fields

| Name | Type | Tags | Description |
|  --- | --- | --- | --- |
| `attempt_id` | `str` | Required | attempt_id from the check<br><br>**Constraints**: *Maximum Length*: `16` |
| `outcome` | [`OutcomeEnum`](../../doc/models/outcome-enum.md) | Required | What PayPal (or the buyer) did |
| `decline_code` | str \| None | Optional | This is a container for any-of cases. |
| `paypal_order_id` | str \| None | Optional | This is a container for any-of cases. |
| `capture_id` | str \| None | Optional | This is a container for any-of cases. |

## Example

```python
from checkoutguard.models.outcome_enum import OutcomeEnum
from checkoutguard.models.outcome_in import OutcomeIn

outcome_in = OutcomeIn(
    attempt_id='attempt_id6',
    outcome=OutcomeEnum.CAPTURED,
    decline_code='String9',
    paypal_order_id='String5',
    capture_id='String7'
)
```

