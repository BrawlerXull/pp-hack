
# Custom Header Signature



Documentation for accessing and setting credentials for GuardKey.

## Auth Credentials

| Name | Type | Description | Getter |
|  --- | --- | --- | --- |
| X-Guard-Key | `str` | A key from the Developers page | `x_guard_key` |



**Note:** Auth credentials can be set using `CustomHeaderAuthenticationCredentials` object, passed in as named parameter `custom_header_authentication_credentials` in the client initialization.

## Usage Example

### Client Initialization

You must provide credentials in the client as shown in the following code snippet.

```python
from checkoutguard.checkout_guard_client import CheckoutGuardClient
from checkoutguard.http.auth.custom_header_authentication import CustomHeaderAuthenticationCredentials

client = CheckoutGuardClient(
    custom_header_authentication_credentials=CustomHeaderAuthenticationCredentials(
        x_guard_key='X-Guard-Key'
    )
)
```


