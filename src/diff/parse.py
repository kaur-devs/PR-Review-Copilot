from __future__ import annotations

import logging
from dataclasses import dataclass, field

from unidiff import PatchSet
from unidiff.errors import UnidiffParseError

from src.diff.files import ChangedFile, ChangedFileSet

logger = logging.getLogger(__name__)

ADDED = "added"
REMOVED = "removed"
CONTEXT = "context"

SIDE_OLD = "LEFT"
SIDE_NEW = "RIGHT"


@dataclass(frozen=True)
class DiffLine:
    content: str
    kind: str
    old_line: int | None
    new_line: int | None

    @property
    def is_added(self) -> bool:
        return self.kind == ADDED

    @property
    def is_removed(self) -> bool:
        return self.kind == REMOVED

    @property
    def side(self) -> str:
        return SIDE_OLD if self.is_removed else SIDE_NEW

    @property
    def comment_line(self) -> int | None:
        return self.old_line if self.is_removed else self.new_line


@dataclass(frozen=True)
class Hunk:
    old_start: int
    old_length: int
    new_start: int
    new_length: int
    lines: list[DiffLine] = field(default_factory=list)

    @property
    def added(self) -> list[DiffLine]:
        return [line for line in self.lines if line.is_added]

    @property
    def removed(self) -> list[DiffLine]:
        return [line for line in self.lines if line.is_removed]

    @property
    def new_range(self) -> range:
        return range(self.new_start, self.new_start + self.new_length)


@dataclass(frozen=True)
class FileDiff:
    filename: str
    hunks: list[Hunk] = field(default_factory=list)
    parse_error: str | None = None

    @property
    def parsed(self) -> bool:
        return self.parse_error is None

    @property
    def added_lines(self) -> list[DiffLine]:
        return [line for hunk in self.hunks for line in hunk.added]

    @property
    def removed_lines(self) -> list[DiffLine]:
        return [line for hunk in self.hunks for line in hunk.removed]

    @property
    def added_line_numbers(self) -> list[int]:
        return [line.new_line for line in self.added_lines if line.new_line]

    @property
    def commentable_lines(self) -> set[int]:
        return {
            line.new_line
            for hunk in self.hunks
            for line in hunk.lines
            if line.new_line is not None and not line.is_removed
        }

    def can_comment_on(self, line_number: int) -> bool:
        return line_number in self.commentable_lines


def ensure_headers(patch: str, filename: str, previous_filename: str | None = None) -> str:
    if patch.lstrip().startswith("diff --git") or patch.lstrip().startswith("---"):
        return patch
    old_name = previous_filename or filename
    return f"--- a/{old_name}\n+++ b/{filename}\n{patch}"


def _build_line(raw) -> DiffLine:
    if raw.is_added:
        kind = ADDED
    elif raw.is_removed:
        kind = REMOVED
    else:
        kind = CONTEXT

    return DiffLine(
        content=raw.value.rstrip("\n"),
        kind=kind,
        old_line=raw.source_line_no,
        new_line=raw.target_line_no,
    )


def parse_patch(
    patch: str, filename: str, previous_filename: str | None = None
) -> FileDiff:
    try:
        patch_set = PatchSet(ensure_headers(patch, filename, previous_filename))
    except (UnidiffParseError, ValueError) as error:
        logger.warning("could not parse the diff for %s: %s", filename, error)
        return FileDiff(filename=filename, parse_error=str(error))

    if not patch_set or not len(patch_set[0]):
        logger.warning("the diff for %s contained no hunks", filename)
        return FileDiff(filename=filename, parse_error="no hunks found in the patch")

    hunks = [
        Hunk(
            old_start=raw_hunk.source_start,
            old_length=raw_hunk.source_length,
            new_start=raw_hunk.target_start,
            new_length=raw_hunk.target_length,
            lines=[_build_line(raw_line) for raw_line in raw_hunk],
        )
        for raw_hunk in patch_set[0]
    ]

    return FileDiff(filename=filename, hunks=hunks)


def parse_changed_file(changed: ChangedFile) -> FileDiff:
    if not changed.has_patch:
        return FileDiff(filename=changed.filename, parse_error="no diff was provided")
    return parse_patch(changed.patch, changed.filename, changed.previous_filename)


def parse_changed_files(file_set: ChangedFileSet) -> list[FileDiff]:
    diffs = [parse_changed_file(changed) for changed in file_set.reviewable]

    failed = [diff.filename for diff in diffs if not diff.parsed]
    if failed:
        logger.warning("%s files could not be parsed: %s", len(failed), ", ".join(failed))

    return diffs
