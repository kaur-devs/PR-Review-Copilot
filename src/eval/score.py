from __future__ import annotations

from dataclasses import dataclass, field

from src.context.bundle import ContextBundle
from src.eval.cases import Case, ExpectedFinding
from src.review.findings import Finding

# A bug on line 113 reported at 112 is the same bug. Exact matching is too
# strict to be useful.
LINE_WINDOW = 3


@dataclass
class CaseScore:
    case_id: str
    matched: list[tuple[ExpectedFinding, Finding]] = field(default_factory=list)
    missed: list[ExpectedFinding] = field(default_factory=list)
    extra: list[Finding] = field(default_factory=list)
    context_found: list[str] = field(default_factory=list)
    context_missed: list[str] = field(default_factory=list)
    model_calls: int = 0
    seconds: float = 0.0
    error: str | None = None

    @property
    def recall(self) -> float | None:
        total = len(self.matched) + len(self.missed)
        return len(self.matched) / total if total else None

    @property
    def precision(self) -> float | None:
        total = len(self.matched) + len(self.extra)
        return len(self.matched) / total if total else None

    @property
    def context_recall(self) -> float | None:
        total = len(self.context_found) + len(self.context_missed)
        return len(self.context_found) / total if total else None


def _same_place(expected: ExpectedFinding, finding: Finding) -> bool:
    return (
        expected.file == finding.file
        and abs(expected.line - finding.line) <= LINE_WINDOW
    )


def score_case(
    case: Case, findings: list[Finding], context: ContextBundle
) -> CaseScore:
    score = CaseScore(case_id=case.id)
    unclaimed = list(findings)

    for expected in case.expected:
        match = next((f for f in unclaimed if _same_place(expected, f)), None)
        if match is None:
            score.missed.append(expected)
        else:
            unclaimed.remove(match)
            score.matched.append((expected, match))

    score.extra = unclaimed

    retrieved = set(context.files)
    for wanted in case.expected_context:
        if wanted in retrieved:
            score.context_found.append(wanted)
        else:
            score.context_missed.append(wanted)

    return score


@dataclass
class RunScore:
    label: str
    cases: list[CaseScore] = field(default_factory=list)

    def _totals(self) -> tuple[int, int, int]:
        matched = sum(len(c.matched) for c in self.cases)
        missed = sum(len(c.missed) for c in self.cases)
        extra = sum(len(c.extra) for c in self.cases)
        return matched, missed, extra

    @property
    def recall(self) -> float | None:
        matched, missed, _ = self._totals()
        return matched / (matched + missed) if matched + missed else None

    @property
    def precision(self) -> float | None:
        matched, _, extra = self._totals()
        return matched / (matched + extra) if matched + extra else None

    @property
    def f1(self) -> float | None:
        p, r = self.precision, self.recall
        if not p or not r:
            return None
        return 2 * p * r / (p + r)

    @property
    def context_recall(self) -> float | None:
        found = sum(len(c.context_found) for c in self.cases)
        missed = sum(len(c.context_missed) for c in self.cases)
        return found / (found + missed) if found + missed else None

    @property
    def model_calls(self) -> int:
        return sum(c.model_calls for c in self.cases)

    @property
    def errors(self) -> int:
        return len([c for c in self.cases if c.error])
