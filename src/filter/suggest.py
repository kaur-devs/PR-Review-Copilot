"""Deciding which findings may offer a replacement line.

A wrong comment costs the reader ten seconds. A wrong suggestion costs them
one click, and the broken code is then theirs. So a suggestion passes a
stricter gate than the comment carrying it, and a refused suggestion never
hides the comment: the finding posts as prose instead.

The last two checks are the real ones. The suggestion is applied to the file
as it exists at the reviewed commit, and the result must both compile and sit
at the same indentation as the line it replaced.

Note that this does not use tree-sitter, which the rest of the project does.
tree-sitter is deliberately tolerant: it has to extract symbols from code that
may be broken, so it never raises. Here we want the opposite, and measurably
so. tree-sitter accepts both of these happily:

    def average(values):
        total = sum(values)
    return total / len(values)        <- de-indented out of the function

CPython does not: that is a SyntaxError, and an over-indented line is an
IndentationError. Different job, different tool.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field, replace
from typing import Collection, Mapping

from src.review.findings import SEVERITY_HIGH, Finding

logger = logging.getLogger(__name__)

# Deliberately stricter than the posting gate in src/filter/gate.py, which
# posts high and medium at 0.6. Start here and widen only if the evaluation
# harness shows it refuses everything.
SUGGESTABLE_SEVERITIES = frozenset({SEVERITY_HIGH})
MINIMUM_CONFIDENCE = 0.8

# A suggestion is posted inside a fenced block, so a suggestion containing a
# fence would close it early and spill the rest into the comment as prose.
FENCE = "```"


def suggestions_enabled() -> bool:
    return os.getenv("SUGGESTIONS_ENABLED", "1").lower() not in (
        "0", "false", "no", "off"
    )


@dataclass(frozen=True)
class SuggestionDecision:
    allow: bool
    reason: str = ""


@dataclass
class SuggestionOutcome:
    """Findings with their suggestions settled, and why any were dropped."""

    findings: list[Finding] = field(default_factory=list)
    refused: list[tuple[Finding, str]] = field(default_factory=list)

    @property
    def offered(self) -> int:
        return sum(1 for finding in self.findings if finding.suggestion)


def apply_to_source(source: str, line: int, suggestion: str) -> str | None:
    """The file with that one line replaced, or None if the line is not there.

    One line in, any number of lines out. That is how an insertion works: the
    model is told to put the original line back inside its replacement, so
    adding a guard above it comes out as two lines where there was one.
    """
    lines = source.splitlines()
    if not 1 <= line <= len(lines):
        return None

    return "\n".join(lines[: line - 1] + suggestion.splitlines() + lines[line:])


def is_valid_python(source: str) -> bool:
    """Whether CPython would accept this as a module.

    compile() parses and does not run anything. IndentationError and TabError
    are both SyntaxError, so one except covers all three.
    """
    try:
        compile(source, "<suggestion>", "exec")
    except SyntaxError:
        return False
    except ValueError:
        # A null byte, or source too deeply nested. Not valid either.
        return False
    return True


def _indentation(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


def keeps_its_place(source: str, line: int, suggestion: str) -> SuggestionDecision:
    """Whether the replacement starts at the same indentation as what it replaces.

    Needed on top of compiling, because a replacement can be shifted out of a
    nested block and still compile. Moving `b = 2` out of an `if` is valid
    Python and a silent behaviour change, which is the worst kind of thing to
    put behind a one-click button.
    """
    lines = source.splitlines()
    original = lines[line - 1]

    first = next((text for text in suggestion.splitlines() if text.strip()), None)
    if first is None:
        return SuggestionDecision(False, "the suggestion is blank")

    if _indentation(first) != _indentation(original):
        return SuggestionDecision(
            False, "it is indented differently from the line it replaces"
        )

    return SuggestionDecision(True)


def survives_parsing(source: str, line: int, suggestion: str) -> SuggestionDecision:
    if not is_valid_python(source):
        # Without a clean starting point there is nothing to compare against,
        # so we cannot blame the suggestion either way. Refuse rather than
        # guess: an unverified suggestion should not get an Apply button.
        return SuggestionDecision(False, "the file does not parse as it is")

    patched = apply_to_source(source, line, suggestion)
    if patched is None:
        return SuggestionDecision(False, f"line {line} is not in the file")

    if not is_valid_python(patched):
        return SuggestionDecision(False, "applying it would stop the file parsing")

    return keeps_its_place(source, line, suggestion)


def passes_cheap_checks(
    finding: Finding, *, unverified: Collection[Finding] = ()
) -> SuggestionDecision:
    """Everything decidable without reading the file.

    Separate from the parse check so a caller can fetch file contents only for
    the findings that got this far, which on a normal pull request is none.
    """
    if not finding.suggestion:
        return SuggestionDecision(False, "no suggestion was offered")

    if not suggestions_enabled():
        return SuggestionDecision(False, "suggestions are switched off")

    if FENCE in finding.suggestion:
        return SuggestionDecision(False, "it contains a code fence")

    if finding.severity not in SUGGESTABLE_SEVERITIES:
        return SuggestionDecision(
            False, f"{finding.severity} severity does not carry a suggestion"
        )

    if finding.confidence < MINIMUM_CONFIDENCE:
        return SuggestionDecision(
            False, f"confidence {finding.confidence} is below {MINIMUM_CONFIDENCE}"
        )

    if finding in unverified:
        return SuggestionDecision(False, "the judge could not confirm the finding")

    return SuggestionDecision(True)


def files_to_read(
    findings: list[Finding], *, unverified: Collection[Finding] = ()
) -> set[str]:
    """Which files a caller must fetch before settle() can decide."""
    return {
        finding.file
        for finding in findings
        if passes_cheap_checks(finding, unverified=unverified).allow
    }


def settle(
    findings: list[Finding],
    *,
    sources: Mapping[str, str] | None = None,
    unverified: Collection[Finding] = (),
) -> SuggestionOutcome:
    """Clear the suggestion on any finding that may not offer one."""
    sources = sources or {}
    outcome = SuggestionOutcome()

    for finding in findings:
        decision = passes_cheap_checks(finding, unverified=unverified)

        if decision.allow:
            source = sources.get(finding.file)
            if source is None:
                decision = SuggestionDecision(
                    False, "the file could not be read, so it was not checked"
                )
            else:
                decision = survives_parsing(
                    source, finding.line, finding.suggestion or ""
                )

        if decision.allow:
            outcome.findings.append(finding)
            continue

        if finding.suggestion:
            outcome.refused.append((finding, decision.reason))

        outcome.findings.append(replace(finding, suggestion=None))

    logger.info(
        "suggestions: %s offered, %s refused", outcome.offered, len(outcome.refused)
    )
    return outcome
