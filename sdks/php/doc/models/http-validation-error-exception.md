
# HTTP Validation Error Exception

## Structure

`HTTPValidationErrorException`

## Fields

| Name | Type | Tags | Description | Getter | Setter |
|  --- | --- | --- | --- | --- | --- |
| `detail` | [`?(ValidationError[])`](../../doc/models/validation-error.md) | Optional | - | getDetail(): ?array | setDetail(?array detail): void |

## Example

```php
try {
    // make the API call
} catch (HTTPValidationErrorException $exp) {
    echo 'Caught HTTPValidationErrorException:', $exp;
} catch (ApiException $exp) {
    echo 'Caught ApiException:', $exp;
}
```

