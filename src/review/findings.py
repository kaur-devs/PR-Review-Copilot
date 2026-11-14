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
    # Missing awaits and unguarded shared state are not error handling.
    "concurrency",
    "security",
    "breaking_change",
    "performance",
    "maintainability",
)

# What models call these categories when they do not use our words. A
# finding was thrown away in review 18 for saying "resource_management"
# about an httpx client that is never closed: the bug was real, the line
# was right, and only the label was wrong. Nothing downstream branches on
# category, so a near-miss is worth mapping rather than deleting.
CATEGORY_SYNONYMS = {
    "resource_management": "error_handling",
    "resource_leak": "error_handling",
    "resources": "error_handling",
    "exception_handling": "error_handling",
    "error": "error_handling",
    "exception": "error_handling",
    "robustness": "error_handling",
    "null_safety": "error_handling",
    "race_condition": "concurrency",
    "thread_safety": "concurrency",
    "async": "concurrency",
    "asynchronous": "concurrency",
    "data_race": "concurrency",
    "vulnerability": "security",
    "injection": "security",
    "sql_injection": "security",
    "xss": "security",
    "authentication": "security",
    "authorization": "security",
    "access_control": "security",
    "secrets": "security",
    "privacy": "security",
    "api_contract": "breaking_change",
    "compatibility": "breaking_change",
    "breaking": "breaking_change",
    "regression": "breaking_change",
    "efficiency": "performance",
    "optimization": "performance",
    "memory": "performance",
    "memory_leak": "performance",
    "scalability": "performance",
    "style": "maintainability",
    "readability": "maintainability",
    "naming": "maintainability",
    "documentation": "maintainability",
    "dead_code": "maintainability",
    "logic": "correctness",
    "logic_error": "correctness",
    "bug": "correctness",
    "type_safety": "correctness",
    "validation": "correctness",
    "input_validation": "correctness",
    "data_integrity": "correctness",
}

# Something is wrong and the model could not name the drawer. Keeping it
# under the broadest category beats deleting a real finding, and the log
# line says which word to add here.
FALLBACK_CATEGORY = "correctness"


def normalise_category(raw: object) -> tuple[str, bool]:
    """A valid category, and whether it had to be translated."""
    name = str(raw or "").strip().lower().replace("-", "_").replace(" ", "_")

    if name in CATEGORIES:
        return name, False
    if name in CATEGORY_SYNONYMS:
        return CATEGORY_SYNONYMS[name], True
    return FALLBACK_CATEGORY, True


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

    # Gated harder than the comment: a wrong one is applied with a click.
    suggestion: str | None = None

    # Where this came from. "Measured, not claimed" needs a finding to say
    # which model and which prompt produced it, or no number quoted about
    # the reviewer can be traced back to the thing that produced it.
    model: str = ""
    found_by: str = ""
    prompt_version: str = ""

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
