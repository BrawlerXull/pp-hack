
# Check Out

What to do with this checkout.

## Structure

`CheckOut`

## Fields

| Name | Type | Tags | Description | Getter | Setter |
|  --- | --- | --- | --- | --- | --- |
| `decision` | [`string(DecisionEnum)`](../../doc/models/decision-enum.md) | Required | allow: go on. allow_with_sca: go on with 3-D Secure. challenge: human check first. block: don't create the order | getDecision(): string | setDecision(string decision): void |
| `reason` | `string` | Required | Why, in a few words (for your logs) | getReason(): string | setReason(string reason): void |
| `incidentId` | string | Required | This is a container for any-of cases. | getIncidentId(): string | setIncidentId(string incidentId): void |
| `scaMethod` | string([ScaMethodEnum](../../doc/models/sca-method-enum.md)) | Required | This is a container for any-of cases. | getScaMethod(): string | setScaMethod(string scaMethod): void |
| `attemptId` | string | Required | This is a container for any-of cases. | getAttemptId(): string | setAttemptId(string attemptId): void |
| `customId` | string | Required | This is a container for any-of cases. | getCustomId(): string | setCustomId(string customId): void |

## Example

```php
use CheckoutGuardLib\Models\Builders\CheckOutBuilder;
use CheckoutGuardLib\Models\DecisionEnum;
use CheckoutGuardLib\Models\ScaMethodEnum;

$checkOut = CheckOutBuilder::init(
    DecisionEnum::CHALLENGE,
    'reason4',
    'String1',
    ScaMethodEnum::SCA_ALWAYS,
    'String3',
    'String9'
)->build();
```

