"""The comment that orients a human reviewer.

A reviewer opening a pull request today sees a handful of inline
comments and nothing else: no sense of what the change does, where the
risk sits, or what was looked at. Both reference designs rank this a
launch requirement, and it is the one feature of theirs this project
did not have.

Deliberately not an agent. The summary describes a change we are already
holding in full, so there is nothing to look up; a tool loop here would
spend turns confirming what is already in the prompt. One call.

It is also the only stage allowed to say something when nothing was
found. Silence reads as a broken bot.
"""

from __future__ import annotations

import logging

from src.agents.answers import read_json_object
from src.agents.triage import risk_notes
from src.classification.kinds import Classification
from src.diff.parse import FileDiff
from src.review.findings import Finding
from src.review.provider import InvalidResponse, LLMError, LLMProvider

logger = logging.getLogger(__name__)

MAX_WHAT_CHANGED = 400
MAX_RISKS = 4
MAX_RISK_LENGTH = 120
MAX_FILES_LISTED = 12

SYSTEM = """You write the opening comment on a pull request review, for a
human who has not read the change yet.

Say what the change does and where the risk is. Be concrete and brief. Do not
list the files; the reader can see them. Do not praise, do not hedge, and do
not repeat the inline comments.

Reply with JSON only:

{"what_changed": "<two or three sentences, plain English>",
 "risks": ["<a short phrase naming an area that deserves attention>"]}

Leave risks empty when nothing stands out. A risk is something a reviewer
should look at, not a problem you are claiming exists."""


def _diff_digest(diffs: list[FileDiff]) -> str:
    lines = []
    for diff in diffs[:MAX_FILES_LISTED]:
        added = len(diff.added_line_numbers)
        lines.append(f"{diff.filename}  (+{added} changed lines)")
        for hunk in diff.hunks:
            for line in hunk.lines:
                if line.is_added:
                    lines.append(f"    + {line.content[:120]}")
    if len(diffs) > MAX_FILES_LISTED:
        lines.append(f"… and {len(diffs) - MAX_FILES_LISTED} more files")
    return "\n".join(lines)


def build_user_prompt(
    diffs: list[FileDiff],
    findings: list[Finding],
    classifications: dict[str, Classification],
    skipped: dict[str, str],
) -> str:
    parts = [f"THE CHANGE ({len(diffs)} reviewable file(s)):", _diff_digest(diffs)]

    notes = risk_notes(classifications)
    if notes:
        parts.append("WHAT IT TOUCHES: " + "; ".join(notes))

    if findings:
        found = ["WHAT THE REVIEWERS FOUND (do not repeat these, just weigh them):"]
        for finding in findings:
            found.append(
                f"  {finding.file}:{finding.line} "
                f"[{finding.severity}] {finding.message}"
            )
        parts.append("\n".join(found))
    else:
        parts.append("WHAT THE REVIEWERS FOUND: nothing.")

    if skipped:
        parts.append(
            "NOT LOOKED AT: " + ", ".join(sorted(skipped)[:MAX_FILES_LISTED])
        )

    return "\n\n".join(parts)


def render(
    what_changed: str,
    risks: list[str],
    findings: list[Finding],
    skipped: dict[str, str],
) -> str:
    """The markdown that goes at the top of the review."""
    lines = [what_changed.strip()] if what_changed.strip() else []

    if risks:
        lines.append("")
        lines.append("**Worth a look**")
        for risk in risks[:MAX_RISKS]:
            lines.append(f"- {risk}")

    counts: dict[str, int] = {}
    for finding in findings:
        counts[finding.severity] = counts.get(finding.severity, 0) + 1

    lines.append("")
    if findings:
        detail = ", ".join(f"{n} {severity}" for severity, n in counts.items())
        lines.append(f"**{len(findings)} comment(s) below**: {detail}.")
    else:
        lines.append("**No issues found.**")

    if skipped:
        lines.append("")
        lines.append(
            f"<sub>Not reviewed: {len(skipped)} file(s) "
            f"({', '.join(sorted(set(skipped.values()))[:3])}).</sub>"
        )

    return "\n".join(lines)


async def summarise(
    model: LLMProvider,
    diffs: list[FileDiff],
    findings: list[Finding],
    classifications: dict[str, Classification],
    skipped: dict[str, str],
) -> str:
    """One call. Returns markdown, or "" when it could not be written.

    Fails quietly: a review with inline comments and no summary is worth
    posting, so nothing here is allowed to stop the review going out.
    """
    if not diffs:
        return ""

    try:
        answer = await model.complete_json(
            SYSTEM,
            build_user_prompt(diffs, findings, classifications, skipped),
        )
    except LLMError as error:
        logger.warning("no summary, the model failed: %s", error)
        return ""

    return _render_answer(answer, findings, skipped)


def _render_answer(answer: object, findings: list[Finding], skipped: dict) -> str:
    if not isinstance(answer, dict):
        logger.warning("the summary model did not return an object")
        return ""

    what = str(answer.get("what_changed", "")).strip()[:MAX_WHAT_CHANGED]

    raw = answer.get("risks")
    risks = (
        [str(item).strip()[:MAX_RISK_LENGTH] for item in raw if str(item).strip()]
        if isinstance(raw, list)
        else []
    )

    if not what and not risks:
        logger.info("the summary model had nothing to say")
        return ""

    return render(what, risks, findings, skipped)


async def summarise_from_text(
    answer_text: str, findings: list[Finding], skipped: dict[str, str]
) -> str:
    """Same rendering, for an answer that arrived as prose rather than JSON."""
    try:
        return _render_answer(read_json_object(answer_text), findings, skipped)
    except InvalidResponse as error:
        logger.warning("unreadable summary: %s", error)
        return ""


__all__ = ["build_user_prompt", "render", "summarise", "summarise_from_text"]
