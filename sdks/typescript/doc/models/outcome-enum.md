
# Outcome Enum

What PayPal (or the buyer) did

## Enumeration

`OutcomeEnum`

## Fields

| Name |
|  --- |
| `Captured` |
| `Declined` |
| `Error` |
| `ScaFailed` |
| `Abandoned` |

## Example

```ts
import { OutcomeEnum } from 'checkoutguard';

const outcome = OutcomeEnum.Declined;
```

