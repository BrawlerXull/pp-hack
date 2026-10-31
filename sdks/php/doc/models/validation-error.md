
# Validation Error

## Structure

`ValidationError`

## Fields

| Name | Type | Tags | Description | Getter | Setter |
|  --- | --- | --- | --- | --- | --- |
| `loc` | array<string\|int> | Required | This is Array of a container for any-of cases. | getLoc(): array | setLoc(array loc): void |
| `msg` | `string` | Required | - | getMsg(): string | setMsg(string msg): void |
| `type` | `string` | Required | - | getType(): string | setType(string type): void |
| `input` | `?array` | Optional | - | getInput(): ?array | setInput(?array input): void |
| `ctx` | `?array` | Optional | - | getCtx(): ?array | setCtx(?array ctx): void |

## Example

```php
use CheckoutGuardLib\Models\Builders\ValidationErrorBuilder;
use CheckoutGuardLib\ApiHelper;

$validationError = ValidationErrorBuilder::init(
    [
        'String9'
    ],
    'msg4',
    'type2'
)
    ->input(ApiHelper::deserialize('{"key1":"val1","key2":"val2"}'))
    ->ctx(ApiHelper::deserialize('{"key1":"val1","key2":"val2"}'))
    ->build();
```

