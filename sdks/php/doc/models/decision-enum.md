
# Decision Enum

allow: go on. allow_with_sca: go on with 3-D Secure. challenge: human check first. block: don't create the order

## Enumeration

`DecisionEnum`

## Fields

| Name |
|  --- |
| `ALLOW` |
| `ALLOW_WITH_SCA` |
| `CHALLENGE` |
| `BLOCK` |

## Example

```php
use CheckoutGuardLib\Models\DecisionEnum;

$decision = DecisionEnum::CHALLENGE;
```

