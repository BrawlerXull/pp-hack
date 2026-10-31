
# Validation Error

## Structure

`ValidationError`

## Fields

| Name | Type | Tags | Description |
|  --- | --- | --- | --- |
| `loc` | List[str \| int] | Required | This is List of a container for any-of cases. |
| `msg` | `str` | Required | - |
| `mtype` | `str` | Required | - |
| `input` | `Any` | Optional | - |
| `ctx` | `Any` | Optional | - |

## Example

```python
import jsonpickle

from checkoutguard.models.validation_error import ValidationError

validation_error = ValidationError(
    loc=[
        'String9',
        'String0',
        'String1'
    ],
    msg='msg4',
    mtype='type2',
    input=jsonpickle.decode('{"key1":"val1","key2":"val2"}'),
    ctx=jsonpickle.decode('{"key1":"val1","key2":"val2"}')
)
```

