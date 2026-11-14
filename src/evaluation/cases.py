from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ExpectedFinding:
    """One defect, and every place it is fair to report it.

    A cross-file defect has two ends. A renamed key can be commented on
    the rename or on the line that still reads the old name; an unguarded
    parse on the function or on its caller. Scoring only one of them
    counts a correct finding as both a miss and a false positive, which
    understates recall and precision at the same time.
    """

    file: str
    line: int
    note: str = ""

    # Other anchors that mean the same defect, as (file, line).
    also: tuple[tuple[str, int], ...] = ()

    @property
    def anchors(self) -> tuple[tuple[str, int], ...]:
        return ((self.file, self.line), *self.also)

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

    # Scored with no model call, so it can be run as often as you like.
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
                        also=tuple(
                            (other["file"], int(other["line"]))
                            for other in item.get("also", [])
                        ),
                    )
                    for item in entry.get("expected", [])
                ),
                expected_context=tuple(entry.get("expected_context", [])),
                note=entry.get("note", ""),
            )
        )
    return cases
