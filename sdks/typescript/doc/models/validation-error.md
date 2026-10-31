
# Validation Error

## Structure

`ValidationError`

## Fields

| Name | Type | Tags | Description |
|  --- | --- | --- | --- |
| `loc` | [`ValidationErrorLoc[]`](../../doc/models/containers/validation-error-loc.md) | Required | This is Array of a container for any-of cases. |
| `msg` | `string` | Required | - |
| `type` | `string` | Required | - |
| `input` | `unknown \| undefined` | Optional | - |
| `ctx` | `unknown \| undefined` | Optional | - |

## Example

```ts
import { ValidationError } from 'checkoutguard';

const validationError: ValidationError = {
  loc: [
    'String9'
  ],
  msg: 'msg4',
  type: 'type2',
  input: { 'key1': 'val1', 'key2': 'val2' },
  ctx: { 'key1': 'val1', 'key2': 'val2' },
};
```

