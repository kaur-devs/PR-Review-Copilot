from __future__ import annotations

import logging
from dataclasses import dataclass, field

from src.github.client import GitHubClient
from src.review.findings import Finding

logger = logging.getLogger(__name__)

EVENT_COMMENT = "COMMENT"
SIDE_NEW = "RIGHT"

MAX_COMMENTS_PER_REVIEW = 25


@dataclass(frozen=True)
class PostedReview:
    github_review_id: int
    html_url: str
    comment_ids: dict[tuple[str, int], int] = field(default_factory=dict)

    def id_for(self, finding: Finding) -> int | None:
        return self.comment_ids.get((finding.file, finding.line))


def build_comment_body(finding: Finding) -> str:
    parts = [f"**{finding.severity} · {finding.category.replace('_', ' ')}**", "", finding.message]

    if finding.rationale and finding.rationale != finding.message:
        parts.extend(["", finding.rationale])

    if finding.evidence_files:
        others = ", ".join(f"`{name}`" for name in sorted(set(finding.evidence_files)))
        parts.extend(["", f"Related code: {others}"])

    parts.extend(["", f"<sub>confidence {finding.confidence} · PR Review Copilot</sub>"])
    return "\n".join(parts)


def build_review_body(posted: list[Finding], held: int) -> str:
    if not posted:
        return "No issues found."

    counts: dict[str, int] = {}
    for finding in posted:
        counts[finding.severity] = counts.get(finding.severity, 0) + 1

    summary = ", ".join(f"{n} {severity}" for severity, n in counts.items())
    lines = [f"Found {len(posted)} issue(s): {summary}."]

    if held:
        lines.append("")
        lines.append(
            f"<sub>{held} further finding(s) were recorded but not posted, "
            "being low severity or low confidence.</sub>"
        )

    return "\n".join(lines)


def build_comments(findings: list[Finding]) -> list[dict]:
    return [
        {
            "path": finding.file,
            "line": finding.line,
            "side": SIDE_NEW,
            "body": build_comment_body(finding),
        }
        for finding in findings
    ]


async def fetch_comment_ids(
    github: GitHubClient, repo_full_name: str, pr_number: int, review_id: int
) -> dict[tuple[str, int], int]:
    comments = await github.get_all_pages(
        f"/repos/{repo_full_name}/pulls/{pr_number}/reviews/{review_id}/comments"
    )
    return {
        (comment["path"], comment["line"]): comment["id"]
        for comment in comments
        if comment.get("line") is not None
    }


async def post_review(
    github: GitHubClient,
    repo_full_name: str,
    pr_number: int,
    head_sha: str,
    findings: list[Finding],
    *,
    held_back: int = 0,
) -> PostedReview:
    to_post = findings[:MAX_COMMENTS_PER_REVIEW]
    if len(findings) > MAX_COMMENTS_PER_REVIEW:
        logger.warning(
            "only posting %s of %s findings, the rest would be noise",
            MAX_COMMENTS_PER_REVIEW,
            len(findings),
        )

    payload = {
        "commit_id": head_sha,
        "event": EVENT_COMMENT,
        "body": build_review_body(to_post, held_back),
        "comments": build_comments(to_post),
    }

    response = await github.post(
        f"/repos/{repo_full_name}/pulls/{pr_number}/reviews", payload
    )
    review_id = response["id"]

    logger.info(
        "posted review %s on %s#%s with %s comments",
        review_id, repo_full_name, pr_number, len(to_post),
    )

    comment_ids = await fetch_comment_ids(
        github, repo_full_name, pr_number, review_id
    )

    return PostedReview(
        github_review_id=review_id,
        html_url=response.get("html_url", ""),
        comment_ids=comment_ids,
    )
