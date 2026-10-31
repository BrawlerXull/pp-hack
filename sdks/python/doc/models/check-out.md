
# Check Out

What to do with this checkout.

## Structure

`CheckOut`

## Fields

| Name | Type | Tags | Description |
|  --- | --- | --- | --- |
| `decision` | [`DecisionEnum`](../../doc/models/decision-enum.md) | Required | allow: go on. allow_with_sca: go on with 3-D Secure. challenge: human check first. block: don't create the order |
| `reason` | `str` | Required | Why, in a few words (for your logs) |
| `incident_id` | str | Required | This is a container for any-of cases. |
| `sca_method` | [ScaMethod](../../doc/models/sca-method-enum.md) | Required | This is a container for any-of cases. |
| `attempt_id` | str | Required | This is a container for any-of cases. |
| `custom_id` | str | Required | This is a container for any-of cases. |

## Example

```python
from checkoutguard.models.check_out import CheckOut
from checkoutguard.models.decision_enum import DecisionEnum
from checkoutguard.models.sca_method_enum import ScaMethodEnum

check_out = CheckOut(
    decision=DecisionEnum.ALLOW,
    reason='reason0',
    incident_id='String5',
    sca_method=ScaMethodEnum.SCA_ALWAYS,
    attempt_id='String7',
    custom_id='String3'
)
```

