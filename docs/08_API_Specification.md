# API Specification

This service has **two routes**. The integration surface is GitHub's webhook,
not a REST API, so most of what matters here is the contract with GitHub in
both directions: what we accept, and what we call.

## What we serve

### POST /webhooks/github

Every GitHub delivery arrives here. Handled in order:

1. **Signature.** `X-Hub-Signature-256` is checked against the raw bytes with
   `hmac.compare_digest`. A failure is logged and nothing is stored, so
   unauthenticated input never reaches the database.
2. **Headers.** `X-GitHub-Event` and `X-GitHub-Delivery` must both be present.
3. **Body.** Must parse as JSON, and a pull-request event must carry the
   fields the pipeline needs.
4. **Route.** A review-thread event records an outcome. A pull-request event
   claims a review. Anything else is recorded and ignored.
5. **Reply, then work.** The review is claimed inside the request and the
   actual review runs as a background task, because GitHub allows ten seconds
   and never retries a timeout.

**Events subscribed to**

| Event | Actions acted on |
|---|---|
| `pull_request` | `opened`, `synchronize`, `reopened`, `ready_for_review` |
| `pull_request_review_thread` | `resolved`, `unresolved` |

`synchronize` is a push to the branch. The thread events are the only way the
project learns whether a comment was useful.

**Success responses — all `200`**

Every authenticated delivery gets a `200`, including duplicates and ignored
actions. GitHub treats any non-2XX as a failed delivery, so a `409` for a
correctly handled duplicate would report working behaviour as broken.

The body names what happened:

| Body | Meaning |
|---|---|
| `{"status": "accepted", "review_id", "repository", "pull_request", "head_sha"}` | claimed; the review is now running |
| `{"status": "accepted", "recorded": n, "resolved": bool}` | a thread event matched `n` of our comments |
| `{"status": "duplicate", "delivery": "..."}` | this delivery id was already stored |
| `{"status": "duplicate", "reason": "commit already claimed"}` | another delivery holds this commit |
| `{"status": "ignored", "event", "action"}` | authenticated, recorded, not something to review |
| `{"status": "ignored", ..., "reason": "not our comments"}` | a thread on comments we did not post |

Two different duplicates, deliberately distinguished: the same *message* twice,
and two different messages about the same *commit*. The first is GitHub
retrying; the second is a reopen with no new code.

**Error responses**

```json
{
  "error": {
    "code": "WEBHOOK_SIGNATURE_INVALID",
    "message": "The request could not be verified.",
    "requestId": "req_123"
  }
}
```

| Status | Code | Cause |
|---|---|---|
| `401` | `WEBHOOK_SIGNATURE_INVALID` | signature missing or wrong |
| `400` | `WEBHOOK_HEADERS_MISSING` | no event or delivery header |
| `400` | `WEBHOOK_PAYLOAD_MALFORMED` | body is not JSON |
| `400` | `WEBHOOK_PAYLOAD_INCOMPLETE` | a field the pipeline needs is absent |

The `401` message is vague on purpose: a forger should learn nothing from the
reply.

### GET /health

Returns `{"status": "ok"}`. No database or provider check — it answers whether
the process is up, nothing more.

## Error codes recorded, not returned

A review fails after the `200` has been sent, so these never reach a caller.
They are stored on `reviews.error_code` and are how a failure is diagnosed
afterwards:

| Code | Cause |
|---|---|
| `LLM_NOT_CONFIGURED` | no API key, or the provider is unreachable |
| `COMMENT_POST_FAILED` | findings stored, GitHub refused the review |
| `PIPELINE_UNEXPECTED` | anything else, with the traceback logged |

## What we call

GitHub's REST API, through one client that handles authentication, pagination
and retries.

| Method | Endpoint | Used for |
|---|---|---|
| `POST` | `/app/installations/{id}/access_tokens` | exchange a signed JWT for an installation token |
| `GET` | `/repos/{owner}/{repo}/pulls/{n}/files` | the diff, paginated |
| `GET` | `/repos/{owner}/{repo}/tarball/{sha}` | the repository at the commit under review |
| `GET` | `/repos/{owner}/{repo}/contents/{path}` | a single file, when the tarball is unavailable |
| `POST` | `/repos/{owner}/{repo}/pulls/{n}/reviews` | one batched review: summary plus inline comments |
| `GET` | `/repos/{owner}/{repo}/pulls/{n}/comments` | the comment ids GitHub assigned |

Two of these carry a decision worth stating.

**The tarball, not the contents endpoint.** The agent greps and opens files
while it works, and per-file HTTP would be both slow and a quota problem. The
archive is downloaded once per review, capped at 50 MB with a 1 MB per-file
limit, and extracted to a temporary directory.

**`/pulls/{n}/comments`, not `/reviews/{id}/comments`.** The review-scoped
endpoint answers in the old positional format with `line` always null. The
pull-request-scoped one reports `line` and `side` and names the review each
comment belongs to, which is what lets an outcome be matched back to the
finding that produced it.

The review is posted as a single `COMMENT` event with at most 25 inline
comments. One request rather than one per finding, and `COMMENT` rather than
`REQUEST_CHANGES` because the reviewer never blocks a merge.

## Not built

The original specification listed `GET /repos`, `/repos/:id/reviews`,
`/reviews/:id`, `/reviews/:id/findings`, `/eval-report` and `/quota-report`
under a `/api/v1` prefix. None exist, and no prefix is in use.

They were planned for a dashboard that was never started. `/quota-report` is
the one with a real gap behind it: it cannot be written today, because token
and call counts live in an in-memory counter and are never stored. See the
quota analysis.

The findings and evaluation data are read with SQL and the harness's JSON
report instead. That is adequate for one developer and not adequate for anyone
else, which is the honest reason these are absent rather than a decision that
they were unnecessary.
