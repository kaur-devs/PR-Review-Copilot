"""Verification that can go and look.

Design note: docs/30_Design_Notes.md#src-judge-investigate
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import replace

from src.agents.answers import read_json_object
from src.agents.loop import AgentRun, run_agent
from src.agents.tools import SCHEMAS, Toolbox
from src.diff.parse import FileDiff
from src.judge.verify import (
    CONTRADICTED,
    SUPPORTED,
    UNVERIFIABLE,
    UNVERIFIABLE_PENALTY,
    VERDICTS,
    JudgeResult,
    judge_enabled,
)
from src.review.findings import SEVERITY_HIGH, Finding
from src.llm.provider import InvalidResponse, LLMError, LLMProvider

logger = logging.getLogger(__name__)

# Below this, the generator was already unsure, so the claim is worth
# checking properly. Above it, a confident medium-severity finding gets
# the cheap single call. This threshold is the main cost dial in the
# pipeline: raising it sends more work to the expensive path.
INVESTIGATE_BELOW_CONFIDENCE = 0.75

SYSTEM = """You are checking whether a code review finding is real. You have
tools that read the repository at the commit under review.

Your job is to try to prove each claim WRONG. A reviewer that posts a
confident, incorrect comment does more damage than one that stays quiet, and
you are the last thing standing between the two.

Work by going and looking. If a finding says a caller breaks, open the caller.
If it says a value can be None, find where it is set. Do not reason from the
diff alone when a tool can settle it.

For each finding, answer with one of:
- "supported"    you checked, and the problem is real
- "contradicted" you checked, and it is not a problem. Say what you found.
- "unverifiable" you could not settle it even with the tools

Prefer contradicted over unverifiable when you actually looked and found the
claim does not hold. Prefer unverifiable over supported when you did not check.

Reply with JSON only:

{"verdicts": [
  {"index": <the number given with the finding>,
   "verdict": "supported|contradicted|unverifiable",
   "reason": "<one sentence, naming the file and line you checked>"}
]}"""


def needs_investigation(finding: Finding) -> bool:
    return (
        finding.severity == SEVERITY_HIGH
        or finding.confidence < INVESTIGATE_BELOW_CONFIDENCE
    )


def split(findings: list[Finding]) -> tuple[list[Finding], list[Finding]]:
    """Which findings earn a tool loop, and which get the cheap check."""
    risky = [finding for finding in findings if needs_investigation(finding)]
    rest = [finding for finding in findings if not needs_investigation(finding)]
    return risky, rest


def _prompt_for(findings: list[Finding], diff: FileDiff | None) -> str:
    parts = []

    if diff is not None:
        lines = [f"THE CHANGE IN {diff.filename}:"]
        for hunk in diff.hunks:
            for line in hunk.lines:
                marker = "+" if line.is_added else "-" if line.is_removed else " "
                number = line.new_line or line.old_line or 0
                lines.append(f"{number:>5} {marker}{line.content}")
        parts.append("\n".join(lines))

    claims = ["CLAIMS TO CHECK:"]
    for index, finding in enumerate(findings):
        claims.append(
            f"\n[{index}] {finding.file}:{finding.line}  "
            f"{finding.severity}/{finding.category}  "
            f"(the reviewer was {finding.confidence} sure)\n"
            f"    {finding.message}\n"
            f"    Their reasoning: {finding.rationale}"
        )
    parts.append("\n".join(claims))

    return "\n\n".join(parts)


def _read_verdicts(answer: str, count: int) -> dict[int, tuple[str, str]]:
    verdicts: dict[int, tuple[str, str]] = {}

    raw = read_json_object(answer).get("verdicts")
    if not isinstance(raw, list):
        raise InvalidResponse("no verdicts list in the answer")

    for entry in raw:
        if not isinstance(entry, dict):
            continue
        try:
            index = int(entry.get("index"))
        except (TypeError, ValueError):
            continue
        if not 0 <= index < count:
            logger.info("a verdict for index %s, which was not asked about", index)
            continue

        verdict = entry.get("verdict")
        if verdict not in VERDICTS:
            logger.info("unreadable verdict for index %s: %r", index, verdict)
            verdict = UNVERIFIABLE

        verdicts[index] = (verdict, str(entry.get("reason", ""))[:200])

    return verdicts


async def _investigate_one_file(
    model: LLMProvider,
    toolbox: Toolbox,
    findings: list[Finding],
    diff: FileDiff | None,
) -> tuple[list[Finding], AgentRun, dict[int, tuple[str, str]]]:
    run = await run_agent(
        model,
        toolbox,
        system=SYSTEM,
        user=_prompt_for(findings, diff),
        tools=SCHEMAS,
    )

    if not run.answered:
        logger.warning("the verifier produced nothing: %s", run)
        return findings, run, {}

    try:
        return findings, run, _read_verdicts(run.answer, len(findings))
    except InvalidResponse as error:
        logger.warning("the verifier did not return usable verdicts: %s", error)
        return findings, run, {}


async def investigate(
    model: LLMProvider,
    toolbox: Toolbox,
    findings: list[Finding],
    diffs: list[FileDiff],
) -> tuple[JudgeResult, dict[str, AgentRun]]:
    """Check each finding by going and looking, one loop per file.

    Fails open, exactly as the cheap judge does: a loop that errors or
    answers unusably leaves its findings untouched. Silently discarding
    real findings is worse than keeping a weak one.
    """
    result = JudgeResult()
    runs: dict[str, AgentRun] = {}

    if not findings:
        return JudgeResult(ran=False), runs

    if not judge_enabled():
        logger.info("judge is switched off, keeping all %s findings", len(findings))
        return JudgeResult(kept=list(findings), ran=False), runs

    diffs_by_file = {diff.filename: diff for diff in diffs}
    by_file: dict[str, list[Finding]] = defaultdict(list)
    for finding in findings:
        by_file[finding.file].append(finding)

    for path, group in by_file.items():
        try:
            _, run, verdicts = await _investigate_one_file(
                model, toolbox, group, diffs_by_file.get(path)
            )
        except LLMError as error:
            logger.warning("the verifier failed on %s, keeping them: %s", path, error)
            result.kept.extend(group)
            continue

        runs[path] = run

        for index, finding in enumerate(group):
            verdict, reason = verdicts.get(
                index, (UNVERIFIABLE, "the verifier did not say")
            )

            if verdict == CONTRADICTED:
                result.rejected.append((finding, reason))
                continue

            if verdict == UNVERIFIABLE:
                weakened = replace(
                    finding,
                    confidence=round(finding.confidence * UNVERIFIABLE_PENALTY, 2),
                )
                result.kept.append(weakened)
                result.unverified.append((weakened, reason))
                continue

            result.kept.append(finding)

    logger.info(
        "verifier: %s across %s file(s), %s turns",
        result.summary,
        len(by_file),
        sum(run.turn_count for run in runs.values()),
    )
    return result, runs


def merge(first: JudgeResult, second: JudgeResult) -> JudgeResult:
    """Join the two verification paths back into one result."""
    return JudgeResult(
        kept=first.kept + second.kept,
        rejected=first.rejected + second.rejected,
        unverified=first.unverified + second.unverified,
        ran=first.ran or second.ran,
    )


__all__ = [
    "INVESTIGATE_BELOW_CONFIDENCE",
    "SUPPORTED",
    "investigate",
    "merge",
    "needs_investigation",
    "split",
]
