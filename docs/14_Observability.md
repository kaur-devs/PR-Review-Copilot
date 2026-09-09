# Observability

## Logs
Log:
- Request ID / webhook delivery ID
- Repo ID, PR number
- Review ID
- Processing stage
- LLM provider, model version, and prompt-template version
- Error code

Do not log full PR source code or diffs beyond what's necessary to debug a specific failure.

## Metrics
- Webhook receipt rate and processing latency
- Review success/failure rate
- LLM latency (per stage)
- Token usage and request-quota consumption per review
- Judge-pass rejection rate
- False-positive rate trend (from accepted/rejected outcomes)

## Tracing
Trace: GitHub webhook → FastAPI → Diff Fetcher → Context Gatherer → Review Generator → Judge Pass → Filter → Comment Poster → Postgres.

## Alerts
Trigger alerts for:
- Repeated processing failures.
- GitHub API errors (rate limit, auth failure).
- LLM dependency failure.
- Abnormal request-count spike per review, or approaching a provider's daily quota.
