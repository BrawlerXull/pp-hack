
# Custom Header Signature



Documentation for accessing and setting credentials for GuardKey.

## Auth Credentials

| Name | Type | Description | Setter |
|  --- | --- | --- | --- |
| X-Guard-Key | `string` | A key from the Developers page | `xGuardKey` |



**Note:** Auth credentials can be set using `customHeaderAuthenticationCredentials` object in the client.

## Usage Example

### Client Initialization

You must provide credentials in the client as shown in the following code snippet.

```ts
import { Client } from 'checkoutguard';

const client = new Client({
  customHeaderAuthenticationCredentials: {
    'X-Guard-Key': 'X-Guard-Key'
  },
});
```


