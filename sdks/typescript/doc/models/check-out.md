
# Check Out

What to do with this checkout.

## Structure

`CheckOut`

## Fields

| Name | Type | Tags | Description |
|  --- | --- | --- | --- |
| `decision` | [`DecisionEnum`](../../doc/models/decision-enum.md) | Required | allow: go on. allow_with_sca: go on with 3-D Secure. challenge: human check first. block: don't create the order |
| `reason` | `string` | Required | Why, in a few words (for your logs) |
| `incidentId` | [`CheckOutIncidentId`](../../doc/models/containers/check-out-incident-id.md) | Required | This is a container for any-of cases. |
| `scaMethod` | [`CheckOutScaMethod`](../../doc/models/containers/check-out-sca-method.md) | Required | This is a container for any-of cases. |
| `attemptId` | [`CheckOutAttemptId`](../../doc/models/containers/check-out-attempt-id.md) | Required | This is a container for any-of cases. |
| `customId` | [`CheckOutCustomId`](../../doc/models/containers/check-out-custom-id.md) | Required | This is a container for any-of cases. |

## Example

```ts
import { CheckOut, DecisionEnum, ScaMethodEnum } from 'checkoutguard';

const checkOut: CheckOut = {
  decision: DecisionEnum.Challenge,
  reason: 'reason4',
  incidentId: 'String1',
  scaMethod: ScaMethodEnum.SCAALWAYS,
  attemptId: 'String3',
  customId: 'String9',
};
```

