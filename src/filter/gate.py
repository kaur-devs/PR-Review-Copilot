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


def apply_gate(
    findings: list[Finding], *, minimum_confidence: float = MINIMUM_CONFIDENCE
) -> GateResult:
    result = GateResult()

    for finding in findings:
        if finding.confidence < minimum_confidence:
            result.hold.append(
                (finding, f"confidence {finding.confidence} below {minimum_confidence}")
            )
        elif finding.severity not in POSTABLE_SEVERITIES:
            result.hold.append((finding, f"{finding.severity} severity is not posted"))
        else:
            result.post.append(finding)

    logger.info(
        "gate: %s findings to post, %s held back", len(result.post), len(result.hold)
    )
    return result
