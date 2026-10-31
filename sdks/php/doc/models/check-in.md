
# Check In

One checkout, as the shop's server sees it, before the PayPal order is created.

## Structure

`CheckIn`

## Fields

| Name | Type | Tags | Description | Getter | Setter |
|  --- | --- | --- | --- | --- | --- |
| `sku` | `string` | Required | The item being bought (your product code)<br><br>**Constraints**: *Maximum Length*: `64` | getSku(): string | setSku(string sku): void |
| `method` | [`string(MethodEnum)`](../../doc/models/method-enum.md) | Required | card: card fields; wallet: the PayPal button | getMethod(): string | setMethod(string method): void |
| `fingerprintHash` | `string` | Required | A stable hash of the buyer's device (e.g. SHA-256 of IP + user agent), never the raw values<br><br>**Constraints**: *Minimum Length*: `8`, *Maximum Length*: `64` | getFingerprintHash(): string | setFingerprintHash(string fingerprintHash): void |
| `challengeToken` | string\|null | Optional | This is a container for any-of cases. | getChallengeToken(): ?string | setChallengeToken(?string challengeToken): void |
| `amount` | float\|string\|null | Optional | This is a container for any-of cases. | getAmount(): | setAmount( amount): void |
| `emailDomain` | string\|null | Optional | This is a container for any-of cases. | getEmailDomain(): ?string | setEmailDomain(?string emailDomain): void |
| `clientIp` | string\|null | Optional | This is a container for any-of cases. | getClientIp(): ?string | setClientIp(?string clientIp): void |
| `dryRun` | `?bool` | Optional | Answer as usual but record nothing: for testing an integration. attempt_id and custom_id are null.<br><br>**Default**: `false` | getDryRun(): ?bool | setDryRun(?bool dryRun): void |

## Example

```php
use CheckoutGuardLib\Models\Builders\CheckInBuilder;
use CheckoutGuardLib\Models\MethodEnum;

$checkIn = CheckInBuilder::init(
    'sku6',
    MethodEnum::CARD,
    'fingerprint_hash8'
)
    ->challengeToken(
        'String5'
    )
    ->amount(
        243.54
    )
    ->emailDomain(
        'String9'
    )
    ->clientIp(
        'String9'
    )
    ->dryRun(false)
    ->build();
```

