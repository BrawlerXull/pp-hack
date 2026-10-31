
# Custom Header Signature



Documentation for accessing and setting credentials for GuardKey.

## Auth Credentials

| Name | Type | Description | Setter | Getter |
|  --- | --- | --- | --- | --- |
| X-Guard-Key | `string` | A key from the Developers page | `xGuardKey` | `getXGuardKey()` |



**Note:** Auth credentials can be set using `CustomHeaderAuthenticationCredentialsBuilder::init()` in `customHeaderAuthenticationCredentials` method in the client builder and accessed through `getCustomHeaderAuthenticationCredentials` method in the client instance.

## Usage Example

### Client Initialization

You must provide credentials in the client as shown in the following code snippet.

```php
use CheckoutGuardLib\Authentication\CustomHeaderAuthenticationCredentialsBuilder;
use CheckoutGuardLib\CheckoutGuardClientBuilder;

$client = CheckoutGuardClientBuilder::init()
    ->customHeaderAuthenticationCredentials(
        CustomHeaderAuthenticationCredentialsBuilder::init(
            'X-Guard-Key'
        )
    )
    ->build();
```


