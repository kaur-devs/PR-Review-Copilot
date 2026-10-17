from __future__ import annotations

from src.classification.kinds import CHANGE_KINDS
from src.context.symbols import Symbol
from src.diff.parse import FileDiff
from src.review.provider import CHARACTERS_PER_TOKEN

MAX_PREVIEW_LINES = 12

# Free tiers cap tokens per minute, not per request. A request larger than the
# whole window can never succeed however long we wait, so the only fix is to
# send less at once. Three thousand tokens of files leaves room in the same
# minute for the system prompt, the reply, and the generation calls that
# follow. A 65-file pull request asked for 14,874 tokens in one request and
# was refused outright.
MAX_PROMPT_TOKENS_PER_REQUEST = 3000
MAX_PROMPT_CHARACTERS = MAX_PROMPT_TOKENS_PER_REQUEST * CHARACTERS_PER_TOKEN

SEPARATOR = "\n\n---\n\n"

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


def _symbols_by_file(symbols: list[Symbol]) -> dict[str, list[str]]:
    by_file: dict[str, list[str]] = {}
    for symbol in symbols:
        if not symbol.is_module_level:
            by_file.setdefault(symbol.file, []).append(symbol.qualified_name)
    return by_file


def _block(diff: FileDiff, touched: list[str]) -> str:
    header = f"FILE: {diff.filename}"
    if touched:
        header += f"\nCHANGED: {', '.join(touched)}"
    return f"{header}\nDIFF:\n{_preview(diff)}"


def build_user_prompt(diffs: list[FileDiff], symbols: list[Symbol]) -> str:
    by_file = _symbols_by_file(symbols)
    blocks = [_block(diff, by_file.get(diff.filename, [])) for diff in diffs]
    return SEPARATOR.join(blocks)


def split_into_requests(
    diffs: list[FileDiff],
    symbols: list[Symbol],
    *,
    limit: int | None = None,
) -> list[list[FileDiff]]:
    """Group files so each request fits inside the token window.

    Measured on the real prompt text rather than a file count, because one
    file with many changed symbols is worth several small ones. A file too
    large on its own still goes out alone: the request may be refused, but
    only that file falls back instead of every file.

    The limit is read here rather than taken as a default argument, because a
    default is bound once when the module loads and could not then be changed.
    """
    if limit is None:
        limit = MAX_PROMPT_CHARACTERS

    by_file = _symbols_by_file(symbols)

    requests: list[list[FileDiff]] = []
    current: list[FileDiff] = []
    used = 0

    for diff in diffs:
        size = len(_block(diff, by_file.get(diff.filename, [])))
        if current:
            size += len(SEPARATOR)
        if current and used + size > limit:
            requests.append(current)
            current = []
            used = 0
            size -= len(SEPARATOR)
        current.append(diff)
        used += size

    if current:
        requests.append(current)

    return requests
