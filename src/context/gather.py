from __future__ import annotations

import logging

from src.context.bundle import STATUS_UNAVAILABLE, ContextBundle, assemble
from src.context.references import find_references
from src.context.snapshot import (
    SnapshotTooLarge,
    SnapshotUnavailable,
    open_snapshot,
)
from src.context.symbols import changed_symbols
from src.diff.parse import FileDiff

logger = logging.getLogger(__name__)


async def gather_context(
    repo_full_name: str,
    head_sha: str,
    installation_id: int,
    diffs: list[FileDiff],
    *,
    client=None,
) -> ContextBundle:
    changed_paths = {diff.filename for diff in diffs}

    try:
        async with open_snapshot(
            repo_full_name, head_sha, installation_id, client=client
        ) as snapshot:
            symbols, problems = changed_symbols(snapshot, diffs)

            if not symbols:
                logger.info("no symbols were resolved, nothing to look up")
                return assemble(snapshot, [], problems)

            references = find_references(snapshot, symbols, changed_paths)
            return assemble(snapshot, references, problems)

    except (SnapshotUnavailable, SnapshotTooLarge) as error:
        logger.warning("no context for %s: %s", repo_full_name, error)
        return ContextBundle(
            status=STATUS_UNAVAILABLE,
            problems={repo_full_name: str(error)},
        )
