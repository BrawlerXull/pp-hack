# Guard

```ts
const guardController = new GuardController(client);
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

```ts
async check(
  body: CheckIn,
  requestOptions?: RequestOptions
): Promise<ApiResponse<CheckOut>>
```

## Authentication

This endpoint requires [GuardKey](../../doc/auth/custom-header-signature.md)

## Parameters

| Parameter | Type | Tags | Description |
|  --- | --- | --- | --- |
| `body` | [`CheckIn`](../../doc/models/check-in.md) | Body, Required | - |
| `requestOptions` | `RequestOptions \| undefined` | Optional | Pass additional request options. |

## Response Type

**200**: What to do with this checkout, and the custom_id to put on the PayPal order

This method returns an [`ApiResponse`](../../doc/api-response.md) instance. The `result` property of this instance returns the response data which is of type [`CheckOut`](../../doc/models/check-out.md).

## Example Usage

```ts
const body: CheckIn = {
  sku: 'sku8',
  method: MethodEnum.Card,
  fingerprintHash: 'fingerprint_hash0',
  dryRun: false,
};

try {
  const response = await guardController.check(body);

  // Extracting fully parsed response body.
  console.log(response.result);

  // Extracting response status code.
  console.log(response.statusCode);
  // Extracting response headers.
  console.log(response.headers);
  // Extracting response body of type `string | Stream`
  console.log(response.body);
} catch (error) {
  if (error instanceof ApiError) {
    // Extracting response error status code.
    console.log(error.statusCode);
    // Extracting response error headers.
    console.log(error.headers);
    // Extracting response error body of type `string | Stream`.
    console.log(error.body);
    if (error instanceof HTTPValidationError) {
      console.log(error.result);
    }
  }
}
```

## Errors

| HTTP Status Code | Error Description | Exception Class |
|  --- | --- | --- |
| 422 | Validation Error | [`HTTPValidationError`](../../doc/models/http-validation-error.md) |


# Report Outcome

For what PayPal sends no webhook about (a card refused while creating the order, an error, the buyer
leaving), or when your PayPal app has no webhooks. Only a checkout still waiting for its outcome changes:
a capture or refund a webhook already recorded is never overwritten.

```ts
async reportOutcome(
  body: OutcomeIn,
  requestOptions?: RequestOptions
): Promise<ApiResponse<OutcomeOut>>
```

## Authentication

This endpoint requires [GuardKey](../../doc/auth/custom-header-signature.md)

## Parameters

| Parameter | Type | Tags | Description |
|  --- | --- | --- | --- |
| `body` | [`OutcomeIn`](../../doc/models/outcome-in.md) | Body, Required | - |
| `requestOptions` | `RequestOptions \| undefined` | Optional | Pass additional request options. |

## Response Type

**200**: Successful Response

This method returns an [`ApiResponse`](../../doc/api-response.md) instance. The `result` property of this instance returns the response data which is of type [`OutcomeOut`](../../doc/models/outcome-out.md).

## Example Usage

```ts
const body: OutcomeIn = {
  attemptId: 'attempt_id8',
  outcome: OutcomeEnum.Error,
};

try {
  const response = await guardController.reportOutcome(body);

  // Extracting fully parsed response body.
  console.log(response.result);

  // Extracting response status code.
  console.log(response.statusCode);
  // Extracting response headers.
  console.log(response.headers);
  // Extracting response body of type `string | Stream`
  console.log(response.body);
} catch (error) {
  if (error instanceof ApiError) {
    // Extracting response error status code.
    console.log(error.statusCode);
    // Extracting response error headers.
    console.log(error.headers);
    // Extracting response error body of type `string | Stream`.
    console.log(error.body);
    if (error instanceof HTTPValidationError) {
      console.log(error.result);
    }
  }
}
```

## Errors

| HTTP Status Code | Error Description | Exception Class |
|  --- | --- | --- |
| 422 | Validation Error | [`HTTPValidationError`](../../doc/models/http-validation-error.md) |

