
# Client Class Documentation

The following parameters are configurable for the API Client:

| Parameter | Type | Description |
|  --- | --- | --- |
| http_client_instance | `Union[Session, HttpClientProvider]` | The Http Client passed from the sdk user for making requests |
| override_http_client_configuration | `bool` | The value which determines to override properties of the passed Http Client from the sdk user |
| http_call_back | `HttpCallBack` | The callback value that is invoked before and after an HTTP call is made to an endpoint |
| timeout | `float` | The value to use for connection timeout. <br> **Default: 1** |
| max_retries | `int` | The number of times to retry an endpoint call if it fails. <br> **Default: 0** |
| backoff_factor | `float` | A backoff factor to apply between attempts after the second try. <br> **Default: 2** |
| retry_statuses | `Array of int` | The http statuses on which retry is to be done. <br> **Default: [408, 413, 429, 500, 502, 503, 504, 521, 522, 524, 408, 413, 429, 500, 502, 503, 504, 521, 522, 524]** |
| retry_methods | `Array of string` | The http methods on which retry is to be done. <br> **Default: ["GET", "PUT", "GET", "PUT"]** |
| proxy_settings | [`ProxySettings`](../doc/proxy-settings.md) | Optional proxy configuration to route HTTP requests through a proxy server. |
| custom_header_authentication_credentials | [`CustomHeaderAuthenticationCredentials`](auth/custom-header-signature.md) | The credential object for Custom Header Signature |

The API client can be initialized as follows:

## Code-Based Client Initialization

```python
from checkoutguard.checkout_guard_client import CheckoutGuardClient
from checkoutguard.configuration import Environment
from checkoutguard.http.auth.custom_header_authentication import CustomHeaderAuthenticationCredentials

client = CheckoutGuardClient(
    custom_header_authentication_credentials=CustomHeaderAuthenticationCredentials(
        x_guard_key='X-Guard-Key'
    ),
    environment=Environment.PRODUCTION
)
```

## Environment-Based Client Initialization

```python
from checkoutguard.checkout_guard_client import CheckoutGuardClient

# Specify the path to your .env file if it’s located outside the project’s root directory.
client = CheckoutGuardClient.from_environment(dotenv_path='/path/to/.env')
```

See the [Environment-Based Client Initialization](../doc/environment-based-client-initialization.md) section for details.

## Checkout Guard API Client

The gateway for the SDK. This class acts as a factory for the Controllers and also holds the configuration of the SDK.

## Controllers

| Name | Description |
|  --- | --- |
| guard | Gets GuardController |

