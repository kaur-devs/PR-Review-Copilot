# UX Requirements

## Primary Interface
The primary interface is **GitHub itself** — there is no separate frontend in v1. The product's UX is the quality and placement of PR comments.

## Information Architecture
1. GitHub PR page (primary surface — inline review comments)
2. GitHub App installation/settings page (GitHub-hosted)
3. *(Optional, v2+)* Lightweight web dashboard — installed repos, recent reviews, quota report, eval results

## Primary User Flow
Install app on repo → open/update PR → wait about 40 seconds → review
appears as inline comments, severity-gated, with a one-line fix behind
GitHub's Apply button where the whole fix fits on the line → resolve or
dismiss each comment → the reviewer notices which categories a repository
stops acting on and goes quiet on them.

A repository that wants different thresholds adds `.pr-review.yml`. There
is no dashboard and no settings UI; the settings live in the repository
so the people being commented on own them.

## Key Surface Requirements

### PR Comment Thread (primary surface)
- Each finding: file, line, severity badge, category, plain-language rationale.
- High/medium severity posted by default; low severity suppressed.
- No more than a handful of comments per PR — dedup and filtering keep it readable.

### Optional Dashboard (v2+)
- List of installed repos.
- Recent reviews with severity breakdown.
- Requests-per-review and cumulative quota consumption.
- Eval report (recall / false-positive rate) from the latest harness run.

## States
Every asynchronous step (webhook received, processing, posted, failed) should be distinguishable in logs/dashboard even where there's no UI screen for it.

## Accessibility
- Comment text avoids color-only severity signaling (badge text, not just color).
- If a dashboard is built, it follows standard semantic HTML and keyboard-navigable controls.
