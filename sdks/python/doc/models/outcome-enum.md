
# Outcome Enum

What PayPal (or the buyer) did

## Enumeration

`OutcomeEnum`

## Fields

| Name |
|  --- |
| `CAPTURED` |
| `DECLINED` |
| `ERROR` |
| `SCA_FAILED` |
| `ABANDONED` |

## Example

```python
from checkoutguard.models.outcome_enum import OutcomeEnum

outcome = OutcomeEnum.DECLINED
```

