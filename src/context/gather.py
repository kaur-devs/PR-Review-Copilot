from __future__ import annotations

import logging
import os

from src.context.bundle import STATUS_UNAVAILABLE, ContextBundle, assemble
from src.context.references import find_references
from src.context.snapshot import (
    Snapshot,
    SnapshotTooLarge,
    SnapshotUnavailable,
    open_snapshot,
)
from src.context.symbols import changed_symbols
from src.diff.parse import FileDiff

logger = logging.getLogger(__name__)


def context_enabled() -> bool:
    return os.getenv("CONTEXT_ENABLED", "1").lower() not in ("0", "false", "no", "off")


def _build(snapshot: Snapshot, diffs: list[FileDiff]) -> ContextBundle:
    symbols, problems = changed_symbols(snapshot, diffs)

    if not symbols:
        logger.info("no symbols were resolved, nothing to look up")
        return assemble(snapshot, [], problems, symbols=[])

    references = find_references(
        snapshot, symbols, {diff.filename for diff in diffs}
    )
    return assemble(snapshot, references, problems, symbols=symbols)


async def gather_context(
    repo_full_name: str,
    head_sha: str,
    installation_id: int,
    diffs: list[FileDiff],
    *,
    client=None,
    snapshot: Snapshot | None = None,
) -> ContextBundle:
    """Find the code elsewhere in the repository that uses what changed.

    A caller that already has the snapshot open passes it in. The agents
    need it open for as long as they are looking, and downloading and
    extracting the same commit twice is pure waste.
    """
    if not context_enabled():
        logger.info("connected-file context is switched off")
        return ContextBundle()

    if snapshot is not None:
        return _build(snapshot, diffs)

    try:
        async with open_snapshot(
            repo_full_name, head_sha, installation_id, client=client
        ) as opened:
            return _build(opened, diffs)

    except (SnapshotUnavailable, SnapshotTooLarge) as error:
        logger.warning("no context for %s: %s", repo_full_name, error)
        return ContextBundle(
            status=STATUS_UNAVAILABLE,
            problems={repo_full_name: str(error)},
        )
