from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ExpectedFinding:
    file: str
    line: int
    note: str = ""

    def __str__(self) -> str:
        return f"{self.file}:{self.line} ({self.note})" if self.note else f"{self.file}:{self.line}"


@dataclass(frozen=True)
class Case:
    """One pull request with a known right answer."""

    id: str
    repo: str
    pr: int
    installation_id: int

    # Problems a good reviewer should report.
    expected: tuple[ExpectedFinding, ...] = ()

    # Files the connected-file stage should have retrieved. Scored without
    # any model call, which is why it can be run as often as you like.
    expected_context: tuple[str, ...] = ()

    note: str = ""


def load_cases(path: Path) -> list[Case]:
    raw = json.loads(Path(path).read_text())

    cases = []
    for entry in raw["cases"]:
        cases.append(
            Case(
                id=entry["id"],
                repo=entry["repo"],
                pr=int(entry["pr"]),
                installation_id=int(entry["installation_id"]),
                expected=tuple(
                    ExpectedFinding(
                        file=item["file"],
                        line=int(item["line"]),
                        note=item.get("note", ""),
                    )
                    for item in entry.get("expected", [])
                ),
                expected_context=tuple(entry.get("expected_context", [])),
                note=entry.get("note", ""),
            )
        )
    return cases
