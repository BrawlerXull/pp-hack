
# Outcome Out

The attempt after the report.

## Structure

`OutcomeOut`

## Fields

| Name | Type | Tags | Description | Getter | Setter |
|  --- | --- | --- | --- | --- | --- |
| `attemptId` | `string` | Required | The attempt reported on | getAttemptId(): string | setAttemptId(string attemptId): void |
| `outcome` | `string` | Required | The attempt's outcome now (unchanged if a webhook recorded it first) | getOutcome(): string | setOutcome(string outcome): void |

## Example

```php
use CheckoutGuardLib\Models\Builders\OutcomeOutBuilder;

$outcomeOut = OutcomeOutBuilder::init(
    'attempt_id8',
    'outcome2'
)->build();
```

