
# Outcome In

What happened to a checked order.

## Structure

`OutcomeIn`

## Fields

| Name | Type | Tags | Description |
|  --- | --- | --- | --- |
| `attemptId` | `string` | Required | attempt_id from the check<br><br>**Constraints**: *Maximum Length*: `16` |
| `outcome` | [`OutcomeEnum`](../../doc/models/outcome-enum.md) | Required | What PayPal (or the buyer) did |
| `declineCode` | [`OutcomeInDeclineCode \| undefined`](../../doc/models/containers/outcome-in-decline-code.md) | Optional | This is a container for any-of cases. |
| `paypalOrderId` | [`OutcomeInPaypalOrderId \| undefined`](../../doc/models/containers/outcome-in-paypal-order-id.md) | Optional | This is a container for any-of cases. |
| `captureId` | [`OutcomeInCaptureId \| undefined`](../../doc/models/containers/outcome-in-capture-id.md) | Optional | This is a container for any-of cases. |

## Example

```ts
import { OutcomeEnum, OutcomeIn } from 'checkoutguard';

const outcomeIn: OutcomeIn = {
  attemptId: 'attempt_id8',
  outcome: OutcomeEnum.Error,
  declineCode: 'String7',
  paypalOrderId: 'String7',
  captureId: 'String9',
};
```

