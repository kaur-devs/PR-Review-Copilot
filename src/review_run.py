from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from src.classification.classify import classify
from src.classification.kinds import Classification
from src.common import trace
from src.context.bundle import ContextBundle
from src.context.gather import gather_context
from src.diff.files import ChangedFileSet, fetch_changed_files
from src.diff.parse import FileDiff, parse_changed_files
from src.diff.skip import worth_reviewing
from src.github.client import GitHubClient
from src.judge.verify import JudgeResult, judge_findings, judge_model_name
from src.review.findings import Finding
from src.review.generate import generate
from src.review.provider import LLMNotConfigured, LLMProvider, quota
from src.webhooks.events import PullRequestEvent

logger = logging.getLogger(__name__)

SKIP_NOTHING_TO_REVIEW = "nothing worth reviewing"
SKIP_NO_MODEL = "no model configured"


@dataclass
class ReviewOutcome:
    """Everything one review produced, with nothing stored or posted.

    The pipeline wraps this to record and post. The evaluation harness uses
    it directly, which is why this function touches neither the database nor
    GitHub's comment API.
    """

    findings: list[Finding] = field(default_factory=list)
    diffs: list[FileDiff] = field(default_factory=list)
    context: ContextBundle = field(default_factory=ContextBundle)
    classifications: dict[str, Classification] = field(default_factory=dict)
    judged: JudgeResult = field(default_factory=JudgeResult)
    skipped_files: dict[str, str] = field(default_factory=dict)
    model_calls: int = 0
    seconds: float = 0.0
    skipped: str | None = None

    @property
    def ran(self) -> bool:
        return self.skipped is None


async def review_pull_request(
    event: PullRequestEvent, *, github_client=None, llm_client=None
) -> ReviewOutcome:
    started = time.monotonic()
    calls_before = quota.requests
    outcome = ReviewOutcome()

    async with GitHubClient(event.installation_id, client=github_client) as github:
        file_set = await fetch_changed_files(
            github, event.repo_full_name, event.pr_number
        )

    keep, skipped = worth_reviewing(file_set.files)
    outcome.skipped_files = skipped
    outcome.diffs = [
        diff for diff in parse_changed_files(ChangedFileSet(files=keep)) if diff.parsed
    ]

    trace.section(
        "CHANGED FILES",
        f"{len(file_set)} changed · {len(keep)} reviewable · {len(skipped)} skipped"
        + (" · TRUNCATED BY GITHUB" if file_set.truncated else ""),
    )
    for changed in keep:
        trace.item(f"{changed.filename}   +{changed.additions} -{changed.deletions}")
    for filename, reason in skipped.items():
        trace.item(f"{filename}   skipped: {reason}")

    if outcome.diffs:
        trace.section(
            "DIFF",
            f"{sum(len(d.added_line_numbers) for d in outcome.diffs)} changed lines",
        )
        for diff in outcome.diffs:
            trace.item(diff.filename)
            trace.diff(diff)

    if not outcome.diffs:
        outcome.skipped = SKIP_NOTHING_TO_REVIEW
        outcome.seconds = time.monotonic() - started
        return outcome

    outcome.context = await gather_context(
        event.repo_full_name,
        event.head_sha,
        event.installation_id,
        outcome.diffs,
        client=github_client,
    )

    trace.section("CHANGED SYMBOLS", f"{len(outcome.context.symbols)} found")
    for symbol in outcome.context.symbols:
        trace.item(
            f"{symbol.kind:<8} {symbol.qualified_name:<34} "
            f"{symbol.file}:{symbol.start_line}-{symbol.end_line}"
        )

    trace.section(
        "CONNECTED CODE",
        f"{len(outcome.context.files)} files · {outcome.context.characters} chars "
        f"· status {outcome.context.status}",
    )
    for item in outcome.context.items:
        trace.item(f"{item.file}:{item.start_line}-{item.end_line}")
        trace.item(
            f"uses {item.changed_symbol} · {item.relation} · "
            f"{item.discovery} · {item.confidence}",
            level=2,
        )
        trace.snippet(item.snippet)
    for filename, reason in outcome.context.problems.items():
        trace.item(f"{filename}   not analysed: {reason}")

    try:
        provider = LLMProvider.from_env(client=llm_client)
    except LLMNotConfigured as error:
        logger.warning("cannot review: %s", error)
        outcome.skipped = SKIP_NO_MODEL
        outcome.seconds = time.monotonic() - started
        return outcome

    async with provider as model:
        outcome.classifications = await classify(
            model, outcome.diffs, outcome.context.symbols
        )

        trace.section("CLASSIFICATION", f"{len(outcome.classifications)} files")
        for diff in outcome.diffs:
            label = outcome.classifications.get(diff.filename)
            if label:
                trace.item(f"{diff.filename:<44} {label.kind:<12} ({label.source})")

        trace.section("FINDINGS", "")
        generated = await generate(
            model, outcome.diffs, outcome.context, outcome.classifications
        )

    for finding in generated:
        trace.finding(finding)

    outcome.judged = await judge_findings(
        generated, outcome.diffs, outcome.context, client=llm_client
    )

    trace.section(
        "JUDGE",
        f"{judge_model_name()} · {outcome.judged.summary}"
        if outcome.judged.ran
        else "not run",
    )
    for finding, reason in outcome.judged.rejected:
        trace.item(f"rejected  {finding.file}:{finding.line}  {reason}")
        trace.item(f"would have said: {finding.message}", level=2)

    outcome.findings = outcome.judged.kept
    outcome.model_calls = quota.requests - calls_before
    outcome.seconds = time.monotonic() - started
    return outcome
