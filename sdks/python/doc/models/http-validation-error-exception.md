
# HTTP Validation Error Exception

## Structure

`HTTPValidationErrorException`

## Fields

| Name | Type | Tags | Description |
|  --- | --- | --- | --- |
| `detail` | [`List[ValidationError]`](../../doc/models/validation-error.md) | Optional | - |

## Example

```python
try:
    # make the API call
except HTTPValidationErrorException as e:
    print(e)
except APIException as e:
    print(e)
```

