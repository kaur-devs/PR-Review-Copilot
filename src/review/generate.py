from __future__ import annotations

import logging
from collections import defaultdict

from src.classification.kinds import Classification
from src.common import trace
from src.context.bundle import ContextBundle
from src.diff.parse import FileDiff
from src.review.findings import CATEGORIES, SEVERITIES, Finding
from src.review.prompts import build_system_prompt, build_user_prompt
from src.review.provider import LLMError, LLMProvider

logger = logging.getLogger(__name__)

MAX_FINDINGS_PER_REQUEST = 20
MAX_MESSAGE_LENGTH = 300
MAX_RATIONALE_LENGTH = 600
SKIP_KINDS = frozenset({"docs"})


def _coerce_confidence(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return min(1.0, max(0.0, number))


def _read_finding(
    entry: object, diffs_by_file: dict[str, FileDiff], context: ContextBundle
) -> tuple[Finding | None, str]:
    if not isinstance(entry, dict):
        return None, "not an object"

    path = entry.get("file")
    diff = diffs_by_file.get(path)
    if diff is None:
        return None, "that file is not in this change"

    try:
        line = int(entry.get("line"))
    except (TypeError, ValueError):
        return None, "no usable line number"

    if not diff.can_comment_on(line):
        return None, f"line {line} is not in the diff, GitHub would refuse it"

    severity = entry.get("severity")
    if severity not in SEVERITIES:
        return None, f"unknown severity {severity!r}"

    category = entry.get("category")
    if category not in CATEGORIES:
        return None, f"unknown category {category!r}"

    message = str(entry.get("message", "")).strip()
    if not message:
        return None, "empty message"

    confidence = _coerce_confidence(entry.get("confidence"))
    if confidence is None:
        return None, f"confidence {entry.get('confidence')!r} is not a number"

    evidence = tuple(
        item.file for item in context.items if item.file != path
    )

    return Finding(
        file=path,
        line=line,
        severity=severity,
        category=category,
        message=message[:MAX_MESSAGE_LENGTH],
        rationale=str(entry.get("rationale", "")).strip()[:MAX_RATIONALE_LENGTH],
        confidence=confidence,
        evidence_files=evidence,
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

    findings = []
    for entry in raw[:MAX_FINDINGS_PER_REQUEST]:
        finding, reason = _read_finding(entry, diffs_by_file, context)
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
