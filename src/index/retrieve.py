"""Using the name index during a review.

Three steps, in this order: make sure the index matches the commit under
review, ask it what else mentions the names this change took away, and
turn the answers into context the agents can read.

The index proposes and the snapshot confirms. A candidate's line number
comes from the index, which may have been built from an earlier commit,
so the snippet is always cut from the snapshot that was downloaded for
this review. A stale index then costs a missed candidate, never a wrong
one, and that is the cheaper way to be wrong.
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from src.context.bundle import ContextItem
from src.context.languages import support_for
from src.context.snapshot import Snapshot
from src.diff.parse import FileDiff
from src.index import store
from src.index.names import names_in_diff

logger = logging.getLogger(__name__)

DISCOVERY_NAME_INDEX = "name_index"
RELATION_MENTIONS = "mentions a name this change removed"

# A window, not a file. Context is resent on every turn of every agent,
# so a whole file costs its length multiplied by the turns that follow.
WINDOW = 10

# Per review, across every changed file.
MAX_ITEMS = 6


def _snippet(snapshot: Snapshot, path: str, line: int) -> tuple[int, int, str]:
    try:
        lines = snapshot.read(path).decode("utf-8", "replace").splitlines()
    except (OSError, KeyError):
        return line, line, ""

    start = max(line - WINDOW // 2, 1)
    end = min(line + WINDOW // 2, len(lines))
    numbered = "\n".join(
        f"{number:>5} {lines[number - 1]}" for number in range(start, end + 1)
    )
    return start, end, numbered


def removed_by(diffs: list[FileDiff], snapshot: Snapshot) -> set[str]:
    """Every name this pull request took the role away from."""
    names: set[str] = set()
    for diff in diffs:
        support = support_for(diff.filename)
        if support is None or not diff.parsed:
            continue
        try:
            after = snapshot.read(diff.filename).decode("utf-8", "replace")
        except (OSError, KeyError):
            continue
        names |= names_in_diff(diff, after, support)
    return names


async def ensure_indexed(
    session: AsyncSession,
    repo_id: int,
    snapshot: Snapshot,
    head_sha: str,
    changed: list[str],
) -> None:
    """Build the index on first sight, then keep only what changed fresh."""
    known = await store.indexed_at(session, repo_id)

    if known is None:
        await store.build(session, repo_id, snapshot, head_sha)
        return

    if known != head_sha:
        await store.reindex_files(session, repo_id, snapshot, changed)


async def candidates_for(
    session: AsyncSession,
    repo_id: int,
    snapshot: Snapshot,
    diffs: list[FileDiff],
) -> list[ContextItem]:
    """Context for the far end of anything this change renamed away."""
    names = removed_by(diffs, snapshot)
    if not names:
        return []

    changed = {diff.filename for diff in diffs}
    languages = {
        support_for(diff.filename).family
        for diff in diffs
        if support_for(diff.filename) is not None
    }

    found = await store.lookup(
        session,
        repo_id,
        names,
        exclude_files=changed,
        changed_language=next(iter(languages)) if len(languages) == 1 else None,
        limit=MAX_ITEMS,
    )

    items = []
    for hit in found:
        start, end, snippet = _snippet(snapshot, hit.file, hit.line)
        if not snippet:
            continue
        items.append(
            ContextItem(
                file=hit.file,
                start_line=start,
                end_line=end,
                snippet=snippet,
                relation=RELATION_MENTIONS,
                discovery=DISCOVERY_NAME_INDEX,
                confidence=hit.score,
                changed_symbol=hit.name,
                reference_lines=(hit.line,),
            )
        )

    if items:
        logger.info(
            "name index: %s file(s) still mention %s",
            len(items), ", ".join(sorted(names)[:3]),
        )
    return items
