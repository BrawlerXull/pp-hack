
# Outcome Enum

What PayPal (or the buyer) did

## Enumeration

`OutcomeEnum`

## Fields

| Name |
|  --- |
| `CAPTURED` |
| `DECLINED` |
| `ERROR` |
| `SCA_FAILED` |
| `ABANDONED` |

## Example

```php
use CheckoutGuardLib\Models\OutcomeEnum;

$outcome = OutcomeEnum::DECLINED;
```

