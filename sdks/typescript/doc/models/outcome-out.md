
# Outcome Out

The attempt after the report.

## Structure

`OutcomeOut`

## Fields

| Name | Type | Tags | Description |
|  --- | --- | --- | --- |
| `attemptId` | `string` | Required | The attempt reported on |
| `outcome` | `string` | Required | The attempt's outcome now (unchanged if a webhook recorded it first) |

## Example

```ts
import { OutcomeOut } from 'checkoutguard';

const outcomeOut: OutcomeOut = {
  attemptId: 'attempt_id8',
  outcome: 'outcome2',
};
```

