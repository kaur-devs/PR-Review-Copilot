# API Specification

Base URL: `/api/v1` (internal/dashboard endpoints; the primary integration surface is the GitHub webhook, not a public REST API)

## Webhook
### POST /webhooks/github
Receives GitHub `pull_request` events. Verifies `X-Hub-Signature-256` before processing. Records the delivery and claims the review, then returns `200` within GitHub's 10-second limit; processing continues asynchronously. See the LLD section on webhook event state and idempotency.

Responses:
- `401` — signature missing or invalid. Nothing is stored.
- `200` — every authenticated delivery, **including duplicates and unhandled actions**. The body states the outcome: `accepted`, `duplicate`, or `ignored`.

Duplicates return `200`, not `409`. GitHub treats any non-2XX response as a failed delivery, so a `409` would flag a correctly handled duplicate as a failure.

## Reviews (dashboard-supporting, optional v2)
### GET /repos
Lists installed repositories.

### GET /repos/:id/reviews
Lists recent reviews for a repo.

### GET /reviews/:id
Returns a review's findings and posting status.

### GET /reviews/:id/findings
Returns structured findings for a review.

## Reporting
### GET /eval-report
Returns the latest evaluation harness result (recall, false-positive rate, sample size).

### GET /quota-report
Returns requests-per-review and cumulative free-tier quota consumption, broken down by provider.

## Standard Errors
```json
{
  "error": {
    "code": "WEBHOOK_SIGNATURE_INVALID",
    "message": "The request could not be verified.",
    "requestId": "req_123"
  }
}
```

Use 400 for validation, 401 for signature/authentication failures, 404 for missing resources, 409 for duplicate/idempotency conflicts on internal endpoints (never on the webhook, see above), 500/503 for server/dependency failures.
