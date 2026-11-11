from __future__ import annotations

from dataclasses import dataclass

SEVERITY_HIGH = "high"
SEVERITY_MEDIUM = "medium"
SEVERITY_LOW = "low"
SEVERITIES = (SEVERITY_HIGH, SEVERITY_MEDIUM, SEVERITY_LOW)

SEVERITY_ORDER = {SEVERITY_HIGH: 0, SEVERITY_MEDIUM: 1, SEVERITY_LOW: 2}

CATEGORIES = (
    "correctness",
    "error_handling",
    # Added when the robustness agent kept reporting real problems under
    # this name and having them thrown away. Its prompt asks for missing
    # awaits, blocking calls on async paths and unguarded shared state;
    # calling those "error_handling" was always a stretch.
    "concurrency",
    "security",
    "breaking_change",
    "performance",
    "maintainability",
)


@dataclass(frozen=True)
class Finding:
    file: str
    line: int
    severity: str
    category: str
    message: str
    rationale: str
    confidence: float
    evidence_files: tuple[str, ...] = ()

    # Replacement text for the line this finding sits on, when the model
    # offered one. Carried here but not posted until it passes its own gate:
    # a wrong comment costs the reader ten seconds, a wrong suggestion costs
    # them one click.
    suggestion: str | None = None

    @property
    def sort_key(self) -> tuple[int, float, str, int]:
        return (
            SEVERITY_ORDER.get(self.severity, 9),
            -self.confidence,
            self.file,
            self.line,
        )

    def __str__(self) -> str:
        return f"{self.file}:{self.line} [{self.severity}/{self.category}] {self.message}"
