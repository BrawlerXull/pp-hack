
# Outcome Out

The attempt after the report.

## Structure

`OutcomeOut`

## Fields

| Name | Type | Tags | Description |
|  --- | --- | --- | --- |
| `attempt_id` | `str` | Required | The attempt reported on |
| `outcome` | `str` | Required | The attempt's outcome now (unchanged if a webhook recorded it first) |

## Example

```python
from checkoutguard.models.outcome_out import OutcomeOut

outcome_out = OutcomeOut(
    attempt_id='attempt_id0',
    outcome='outcome4'
)
```

