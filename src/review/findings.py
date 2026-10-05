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
