from __future__ import annotations

from src.classification.kinds import CHANGE_KINDS
from src.context.symbols import Symbol
from src.diff.parse import FileDiff

MAX_PREVIEW_LINES = 12

SYSTEM = f"""You label code changes so a reviewer knows which questions to ask.

Reply with JSON only, in this shape:

{{"files": [{{"file": "<path>", "kind": "<kind>", "reason": "<a few words>"}}]}}

kind must be one of: {", ".join(CHANGE_KINDS)}

What each kind means:
- api: routes, endpoints, request or response shapes, public interfaces
- schema: database models, tables, columns, migrations
- dependency: adding, removing or upgrading a library
- config: settings, environment, build or deployment configuration
- logic: business rules, algorithms, data handling
- test: tests and fixtures
- docs: documentation and comments only

Include every file given to you, exactly once, using the path as written."""


def _preview(diff: FileDiff) -> str:
    lines = []
    for hunk in diff.hunks:
        for line in hunk.lines:
            if line.is_added:
                lines.append(f"+{line.content}")
            elif line.is_removed:
                lines.append(f"-{line.content}")
            if len(lines) >= MAX_PREVIEW_LINES:
                return "\n".join(lines) + "\n..."
    return "\n".join(lines)


def build_user_prompt(
    diffs: list[FileDiff], symbols: list[Symbol]
) -> str:
    by_file: dict[str, list[str]] = {}
    for symbol in symbols:
        if not symbol.is_module_level:
            by_file.setdefault(symbol.file, []).append(symbol.qualified_name)

    blocks = []
    for diff in diffs:
        touched = by_file.get(diff.filename, [])
        header = f"FILE: {diff.filename}"
        if touched:
            header += f"\nCHANGED: {', '.join(touched)}"
        blocks.append(f"{header}\nDIFF:\n{_preview(diff)}")

    return "\n\n---\n\n".join(blocks)
