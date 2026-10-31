
# Decision Enum

allow: go on. allow_with_sca: go on with 3-D Secure. challenge: human check first. block: don't create the order

## Enumeration

`DecisionEnum`

## Fields

| Name |
|  --- |
| `Allow` |
| `AllowWithSca` |
| `Challenge` |
| `Block` |

## Example

```ts
import { DecisionEnum } from 'checkoutguard';

const decision = DecisionEnum.Challenge;
```

