
# Outcome In

What happened to a checked order.

## Structure

`OutcomeIn`

## Fields

| Name | Type | Tags | Description | Getter | Setter |
|  --- | --- | --- | --- | --- | --- |
| `attemptId` | `string` | Required | attempt_id from the check<br><br>**Constraints**: *Maximum Length*: `16` | getAttemptId(): string | setAttemptId(string attemptId): void |
| `outcome` | [`string(OutcomeEnum)`](../../doc/models/outcome-enum.md) | Required | What PayPal (or the buyer) did | getOutcome(): string | setOutcome(string outcome): void |
| `declineCode` | string\|null | Optional | This is a container for any-of cases. | getDeclineCode(): ?string | setDeclineCode(?string declineCode): void |
| `paypalOrderId` | string\|null | Optional | This is a container for any-of cases. | getPaypalOrderId(): ?string | setPaypalOrderId(?string paypalOrderId): void |
| `captureId` | string\|null | Optional | This is a container for any-of cases. | getCaptureId(): ?string | setCaptureId(?string captureId): void |

## Example

```php
use CheckoutGuardLib\Models\Builders\OutcomeInBuilder;
use CheckoutGuardLib\Models\OutcomeEnum;

$outcomeIn = OutcomeInBuilder::init(
    'attempt_id8',
    OutcomeEnum::ERROR
)
    ->declineCode(
        'String7'
    )
    ->paypalOrderId(
        'String7'
    )
    ->captureId(
        'String9'
    )
    ->build();
```

