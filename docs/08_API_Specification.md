# API Specification

Base URL: `/api/v1` (internal/dashboard endpoints; the primary integration surface is the GitHub webhook, not a public REST API)

## Webhook
### POST /webhooks/github
Receives GitHub `pull_request` events. Verifies `X-Hub-Signature-256` before processing. Returns `200` immediately; processing continues asynchronously.

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

### GET /cost-report
Returns cost-per-review and cumulative spend.

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

Use 400 for validation, 401 for signature/authentication failures, 404 for missing resources, 409 for duplicate/idempotency conflicts, 500/503 for server/dependency failures.
