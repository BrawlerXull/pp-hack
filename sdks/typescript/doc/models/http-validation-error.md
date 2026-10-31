
# HTTP Validation Error

## Structure

`HTTPValidationError`

## Fields

| Name | Type | Tags | Description |
|  --- | --- | --- | --- |
| `detail` | [`ValidationError[] \| undefined`](../../doc/models/validation-error.md) | Optional | - |

## Example

```ts
try {
  // make the API call
} catch (error) {
  if (error instanceof HTTPValidationError) {
    console.log(error.result);
  }
}
```

