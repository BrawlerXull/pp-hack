
# Check In

One checkout, as the shop's server sees it, before the PayPal order is created.

## Structure

`CheckIn`

## Fields

| Name | Type | Tags | Description |
|  --- | --- | --- | --- |
| `sku` | `string` | Required | The item being bought (your product code)<br><br>**Constraints**: *Maximum Length*: `64` |
| `method` | [`MethodEnum`](../../doc/models/method-enum.md) | Required | card: card fields; wallet: the PayPal button |
| `fingerprintHash` | `string` | Required | A stable hash of the buyer's device (e.g. SHA-256 of IP + user agent), never the raw values<br><br>**Constraints**: *Minimum Length*: `8`, *Maximum Length*: `64` |
| `challengeToken` | [`CheckInChallengeToken \| undefined`](../../doc/models/containers/check-in-challenge-token.md) | Optional | This is a container for any-of cases. |
| `amount` | [`CheckInAmount \| undefined`](../../doc/models/containers/check-in-amount.md) | Optional | This is a container for any-of cases. |
| `emailDomain` | [`CheckInEmailDomain \| undefined`](../../doc/models/containers/check-in-email-domain.md) | Optional | This is a container for any-of cases. |
| `clientIp` | [`CheckInClientIp \| undefined`](../../doc/models/containers/check-in-client-ip.md) | Optional | This is a container for any-of cases. |
| `dryRun` | `boolean \| undefined` | Optional | Answer as usual but record nothing: for testing an integration. attempt_id and custom_id are null.<br><br>**Default**: `false` |

## Example

```ts
import { CheckIn, MethodEnum } from 'checkoutguard';

const checkIn: CheckIn = {
  sku: 'sku6',
  method: MethodEnum.Card,
  fingerprintHash: 'fingerprint_hash8',
  challengeToken: 'String5',
  amount: 243.54,
  emailDomain: 'String9',
  clientIp: 'String9',
  dryRun: false,
};
```

