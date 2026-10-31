# Guard

```php
$guardController = $client->getGuardController();
```

## Class Name

`GuardController`

## Methods

* [Check](../../doc/controllers/guard.md#check)
* [Report Outcome](../../doc/controllers/guard.md#report-outcome)


# Check

Call this from your server before creating each PayPal order, and do what `decision` says. Put
`custom_id` on the order so PayPal's webhooks for it reach this checkout. If no answer comes within about
a second, go on as allow: the guard is built to fail open.

```php
function check(CheckIn $body): CheckOut
```

## Authentication

This endpoint requires [GuardKey](../../doc/auth/custom-header-signature.md)

## Parameters

| Parameter | Type | Tags | Description |
|  --- | --- | --- | --- |
| `body` | [`CheckIn`](../../doc/models/check-in.md) | Body, Required | - |

## Response Type

**200**: What to do with this checkout, and the custom_id to put on the PayPal order

[`CheckOut`](../../doc/models/check-out.md)

## Example Usage

```php
$body = CheckInBuilder::init(
    'sku8',
    MethodEnum::CARD,
    'fingerprint_hash0'
)
    ->dryRun(false)
    ->build();

$guardController = $client->getGuardController();

try {
    $result = $guardController->check($body);
    echo 'CheckOut:';
    var_dump($result);
} catch (HTTPValidationErrorException $exp) {
    echo 'Caught HTTPValidationErrorException:', $exp;
} catch (ApiException $exp) {
    echo 'Caught:', $exp;
}
```

## Errors

| HTTP Status Code | Error Description | Exception Class |
|  --- | --- | --- |
| 422 | Validation Error | [`HTTPValidationErrorException`](../../doc/models/http-validation-error-exception.md) |


# Report Outcome

For what PayPal sends no webhook about (a card refused while creating the order, an error, the buyer
leaving), or when your PayPal app has no webhooks. Only a checkout still waiting for its outcome changes:
a capture or refund a webhook already recorded is never overwritten.

```php
function reportOutcome(OutcomeIn $body): OutcomeOut
```

## Authentication

This endpoint requires [GuardKey](../../doc/auth/custom-header-signature.md)

## Parameters

| Parameter | Type | Tags | Description |
|  --- | --- | --- | --- |
| `body` | [`OutcomeIn`](../../doc/models/outcome-in.md) | Body, Required | - |

## Response Type

**200**: Successful Response

[`OutcomeOut`](../../doc/models/outcome-out.md)

## Example Usage

```php
$body = OutcomeInBuilder::init(
    'attempt_id8',
    OutcomeEnum::ERROR
)->build();

$guardController = $client->getGuardController();

try {
    $result = $guardController->reportOutcome($body);
    echo 'OutcomeOut:';
    var_dump($result);
} catch (HTTPValidationErrorException $exp) {
    echo 'Caught HTTPValidationErrorException:', $exp;
} catch (ApiException $exp) {
    echo 'Caught:', $exp;
}
```

## Errors

| HTTP Status Code | Error Description | Exception Class |
|  --- | --- | --- |
| 422 | Validation Error | [`HTTPValidationErrorException`](../../doc/models/http-validation-error-exception.md) |

