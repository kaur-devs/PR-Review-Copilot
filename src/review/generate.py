from __future__ import annotations

import logging
from collections import defaultdict

from src.classification.kinds import Classification
from src.common import trace
from src.context.bundle import ContextBundle
from src.diff.parse import FileDiff
from src.review.findings import SEVERITIES, Finding, normalise_category
from src.review.prompts import build_system_prompt, build_user_prompt
from src.llm.provider import LLMError, LLMProvider

logger = logging.getLogger(__name__)

MAX_FINDINGS_PER_REQUEST = 20
MAX_MESSAGE_LENGTH = 300
MAX_RATIONALE_LENGTH = 600

# Longer than this is the model rewriting a function, not fixing a line.
MAX_SUGGESTION_LENGTH = 400
SKIP_KINDS = frozenset({"docs"})


def read_suggestion(value: object) -> str | None:
    """The replacement text, or None when there is nothing usable.

    Deliberately not stripped the way a message is. Leading whitespace is the
    indentation GitHub will paste into the file, so losing it produces code
    that does not compile. Only trailing blank lines go.
    """
    if not isinstance(value, str):
        return None

    text = value.rstrip("\n")
    if not text.strip():
        return None

    return text[:MAX_SUGGESTION_LENGTH]


def _coerce_confidence(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return min(1.0, max(0.0, number))


def _first_commentable_line(diff: FileDiff) -> int | None:
    for line in diff.added_line_numbers:
        if diff.can_comment_on(line):
            return line
    return None


def reanchor(
    entry: dict, diffs_by_file: dict[str, FileDiff]
) -> tuple[str, int, str] | None:
    """Move a finding about an untouched file onto the change that caused it.

    The agentic reviewer can see further than GitHub lets us point. On
    the first real run an agent reported that `chat.py:86` reads keys
    `retrieve_context` no longer returns — a genuine cross-file break, in
    a file the pull request never touched — and it was thrown away,
    because a comment can only be anchored to a changed line.

    Dropping it is the wrong answer: that finding is the whole reason for
    running agents. Instead it is anchored on the changed line that
    caused the problem, and the place it breaks is named in the message,
    which is also where a reader wants the comment to appear.

    Returns the new (file, line, note), or None if nothing in the diff
    can be tied to it.
    """
    named = [
        value
        for key in ("affects", "evidence_files")
        for value in (entry.get(key) or [])
        if isinstance(value, str)
    ]

    for candidate in [*named, *diffs_by_file]:
        path = candidate.split(":", 1)[0]
        diff = diffs_by_file.get(path)
        if diff is None:
            continue
        line = _first_commentable_line(diff)
        if line is None:
            continue
        return path, line, "the cause of a problem in another file"

    return None


def read_finding(
    entry: object,
    diffs_by_file: dict[str, FileDiff],
    evidence: tuple[str, ...] = (),
    *,
    allow_reanchor: bool = False,
) -> tuple[Finding | None, str]:
    """Turn one entry from a model into a Finding, or say why not.

    Shared with the specialist agents, which produce the same shape from a
    different prompt. Evidence is passed in rather than derived: the
    context builder knows which files it supplied, and an agent knows
    which files it actually opened.

    `allow_reanchor` is for findings from an agent, which can reach files
    the diff does not contain. The single-pass generator only ever sees
    the diff, so a path outside it there is a mistake, not a discovery.
    """
    if not isinstance(entry, dict):
        return None, "not an object"

    path = entry.get("file")
    diff = diffs_by_file.get(path)
    affects = ""

    if diff is None:
        if not allow_reanchor:
            return None, "that file is not in this change"

        moved = reanchor(entry, diffs_by_file)
        if moved is None:
            return None, "that file is not in this change, and nothing in it fits"

        affects = f"{path}:{entry.get('line')}" if path else ""
        path, reanchored_line, _ = moved
        diff = diffs_by_file[path]
        entry = {**entry, "file": path, "line": reanchored_line}
        logger.info("moved a finding about %s onto %s:%s", affects, path, reanchored_line)

    try:
        line = int(entry.get("line"))
    except (TypeError, ValueError):
        return None, "no usable line number"

    if not diff.can_comment_on(line):
        return None, f"line {line} is not in the diff, GitHub would refuse it"

    severity = entry.get("severity")
    if severity not in SEVERITIES:
        return None, f"unknown severity {severity!r}"

    category, translated = normalise_category(entry.get("category"))
    if translated:
        logger.info(
            "category %r is not one of ours, filing it under %r",
            entry.get("category"), category,
        )

    message = str(entry.get("message", "")).strip()
    if not message:
        return None, "empty message"

    # The comment sits on the cause, so name where it actually breaks.
    if affects:
        message = f"{message} (breaks {affects})"

    confidence = _coerce_confidence(entry.get("confidence"))
    if confidence is None:
        return None, f"confidence {entry.get('confidence')!r} is not a number"

    return Finding(
        file=path,
        line=line,
        severity=severity,
        category=category,
        message=message[:MAX_MESSAGE_LENGTH],
        rationale=str(entry.get("rationale", "")).strip()[:MAX_RATIONALE_LENGTH],
        confidence=confidence,
        evidence_files=evidence,
        suggestion=read_suggestion(entry.get("suggestion")),
    ), ""


def _group_by_kind(
    diffs: list[FileDiff], classifications: dict[str, Classification]
) -> dict[str, list[FileDiff]]:
    grouped: dict[str, list[FileDiff]] = defaultdict(list)

    for diff in diffs:
        classification = classifications.get(diff.filename)
        kind = classification.kind if classification else "logic"
        if kind in SKIP_KINDS:
            logger.info("not reviewing %s, it is %s", diff.filename, kind)
            continue
        grouped[kind].append(diff)

    return grouped


async def _generate_for_kind(
    model: LLMProvider,
    kind: str,
    diffs: list[FileDiff],
    context: ContextBundle,
) -> list[Finding]:
    diffs_by_file = {diff.filename: diff for diff in diffs}

    try:
        answer = await model.complete_json(
            build_system_prompt(kind), build_user_prompt(diffs, context)
        )
    except LLMError as error:
        logger.warning("no findings for the %s files: %s", kind, error)
        return []

    raw = answer.get("findings")
    if not isinstance(raw, list):
        logger.warning("the model did not return a findings list for %s", kind)
        return []

    evidence = tuple(item.file for item in context.items)

    findings = []
    for entry in raw[:MAX_FINDINGS_PER_REQUEST]:
        finding, reason = read_finding(
            entry, diffs_by_file, tuple(f for f in evidence if f != entry.get("file"))
            if isinstance(entry, dict) else evidence,
        )
        if finding is not None:
            findings.append(finding)
        else:
            logger.info("dropped a finding for %s files: %s", kind, reason)
            if isinstance(entry, dict):
                trace.rejected(entry, reason)

    logger.info(
        "%s files: %s findings kept out of %s returned", kind, len(findings), len(raw)
    )
    return findings


async def generate(
    model: LLMProvider,
    diffs: list[FileDiff],
    context: ContextBundle,
    classifications: dict[str, Classification],
) -> list[Finding]:
    grouped = _group_by_kind(diffs, classifications)

    if not grouped:
        return []

    findings: list[Finding] = []
    for kind, kind_diffs in grouped.items():
        findings.extend(await _generate_for_kind(model, kind, kind_diffs, context))

    findings.sort(key=lambda finding: finding.sort_key)

    logger.info(
        "%s findings from %s requests", len(findings), len(grouped)
    )
    return findings
