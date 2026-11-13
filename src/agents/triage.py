"""Which specialists are worth waking for this pull request.

Design note: docs/30_Design_Notes.md#src-agents-triage
"""

from __future__ import annotations

import logging

from src.agents.prompts import ROBUST, WRONG
from src.classification.kinds import (
    API,
    CONFIG,
    DEPENDENCY,
    DOCS,
    LOGIC,
    SCHEMA,
    TEST,
    Classification,
)

logger = logging.getLogger(__name__)

# What each kind of change could plausibly contain.
WAKES: dict[str, tuple[str, ...]] = {
    # Behaviour can be wrong and can fail badly.
    LOGIC: (WRONG, ROBUST),
    API: (WRONG, ROBUST),
    SCHEMA: (WRONG, ROBUST),

    # A leaked secret or changed default; no error handling to get wrong.
    CONFIG: (WRONG,),
    DEPENDENCY: (WRONG,),

    # The robustness reviewer is the one told to look for a weakened test.
    TEST: (ROBUST,),

    DOCS: (),
}

# Said plainly in the summary so a human knows where to look.
RISKY_KINDS = (SCHEMA, API)


def specialists_for(classifications: dict[str, Classification]) -> tuple[str, ...]:
    """The specialists worth running, in a stable order.

    The union across every changed file: one logic file in a pull request
    full of documentation still earns a full review.
    """
    wanted: set[str] = set()
    for classification in classifications.values():
        wanted.update(WAKES.get(classification.kind, (WRONG, ROBUST)))

    chosen = tuple(name for name in (WRONG, ROBUST) if name in wanted)

    logger.info(
        "triage: %s file(s) -> %s",
        len(classifications),
        ", ".join(chosen) or "no specialist, nothing here to review",
    )
    return chosen


def risk_notes(classifications: dict[str, Classification]) -> list[str]:
    """Short phrases for the summary, naming what the change touches."""
    kinds = {c.kind for c in classifications.values()}
    notes = []

    if SCHEMA in kinds:
        notes.append("changes the database schema")
    if API in kinds:
        notes.append("changes a public interface")
    if DEPENDENCY in kinds:
        notes.append("changes dependencies")
    if CONFIG in kinds:
        notes.append("changes configuration")
    if LOGIC in kinds and TEST not in kinds:
        notes.append("changes behaviour with no test touched")

    return notes


__all__ = ["RISKY_KINDS", "WAKES", "risk_notes", "specialists_for"]
