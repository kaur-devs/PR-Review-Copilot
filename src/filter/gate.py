from __future__ import annotations

import logging
from dataclasses import dataclass, field

from src.review.findings import SEVERITY_HIGH, SEVERITY_MEDIUM, Finding

logger = logging.getLogger(__name__)

POSTABLE_SEVERITIES = frozenset({SEVERITY_HIGH, SEVERITY_MEDIUM})
MINIMUM_CONFIDENCE = 0.6


@dataclass
class GateResult:
    post: list[Finding] = field(default_factory=list)
    hold: list[tuple[Finding, str]] = field(default_factory=list)

    @property
    def held_reasons(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for _, reason in self.hold:
            counts[reason] = counts.get(reason, 0) + 1
        return counts


def severity_gate(findings: list[Finding]) -> GateResult:
    """Drop what will never be posted, before anything is spent on it.

    This half runs *before* verification, because severity is decided by
    the finding itself and does not change. Verifying a low-severity
    candidate that the gate will drop anyway is the most straightforward
    waste in the pipeline: in the agentic design verification carries most
    of the bill, and it is the only stage that cannot be redone cheaply.

    Confidence deliberately stays behind, in confidence_gate. Verification
    is what sets the final confidence, so checking it first would throw
    away findings before they have been looked at properly.
    """
    result = GateResult()

    for finding in findings:
        if finding.severity not in POSTABLE_SEVERITIES:
            result.hold.append((finding, f"{finding.severity} severity is not posted"))
        else:
            result.post.append(finding)

    logger.info(
        "severity gate: %s worth checking, %s dropped",
        len(result.post),
        len(result.hold),
    )
    return result


def confidence_gate(
    findings: list[Finding], *, minimum_confidence: float = MINIMUM_CONFIDENCE
) -> GateResult:
    """Drop what we are not sure enough about, after verification.

    Run last, because the judge is what moves a confidence: a finding it
    could not check is multiplied by the unverifiable penalty and may land
    below the floor it was above a moment ago.
    """
    result = GateResult()

    for finding in findings:
        if finding.confidence < minimum_confidence:
            result.hold.append(
                (finding, f"confidence {finding.confidence} below {minimum_confidence}")
            )
        else:
            result.post.append(finding)

    logger.info(
        "confidence gate: %s to post, %s held back",
        len(result.post),
        len(result.hold),
    )
    return result


def apply_gate(
    findings: list[Finding], *, minimum_confidence: float = MINIMUM_CONFIDENCE
) -> GateResult:
    """Both halves at once, for the single-pass pipeline.

    Confidence is checked first here so that a finding failing both bars
    reports the more specific reason. That ordering only decides which
    reason is printed, not what is posted.

    The agentic pipeline calls the two halves separately instead, running
    severity before it pays to verify anything.
    """
    by_confidence = confidence_gate(findings, minimum_confidence=minimum_confidence)
    by_severity = severity_gate(by_confidence.post)

    reasons = {
        id(finding): reason
        for finding, reason in by_confidence.hold + by_severity.hold
    }

    return GateResult(
        post=by_severity.post,
        hold=[
            (finding, reasons[id(finding)])
            for finding in findings
            if id(finding) in reasons
        ],
    )
