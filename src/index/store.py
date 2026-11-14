"""Building the name index, keeping it current, and asking it questions."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.context.languages import support_for
from src.context.snapshot import Snapshot
from src.db.models import RepositoryIndex, SymbolReference
from src.index.names import KIND_WEIGHT, names_in_file

logger = logging.getLogger(__name__)

# Inserted in batches so one enormous repository does not build a single
# statement too large to send.
BATCH = 2_000

# A name in this many files is a word, not a reference.
TOO_WIDESPREAD = 30


@dataclass(frozen=True)
class Candidate:
    """A file that mentions a name the diff changed."""

    file: str
    line: int
    kind: str
    language: str
    name: str
    score: float

    def __str__(self) -> str:
        return f"{self.file}:{self.line} ({self.name}, {self.kind}, {self.score:.2f})"


def _rows(repo_id: int, path: str, snapshot: Snapshot) -> list[dict]:
    support = support_for(path)
    if support is None:
        return []
    return [
        {
            "repo_id": repo_id,
            "name": name.text,
            "file_path": path,
            "line": name.line,
            "kind": name.kind,
            "language": support.family,
        }
        for name in names_in_file(snapshot, path)
    ]


async def _insert(session: AsyncSession, rows: list[dict]) -> int:
    for start in range(0, len(rows), BATCH):
        await session.execute(insert(SymbolReference), rows[start:start + BATCH])
    return len(rows)


async def reindex_files(
    session: AsyncSession, repo_id: int, snapshot: Snapshot, paths: list[str]
) -> int:
    """Replace what we know about these files.

    Delete and re-insert rather than reconcile, because nothing here was
    resolved: a row is only "this name, this file, this line", so there
    are no relationships elsewhere to repair. That is the whole reason
    this is cheaper to keep current than a resolved graph would be.
    """
    if not paths:
        return 0

    await session.execute(
        delete(SymbolReference).where(
            SymbolReference.repo_id == repo_id,
            SymbolReference.file_path.in_(paths),
        )
    )

    rows: list[dict] = []
    for path in paths:
        rows.extend(_rows(repo_id, path, snapshot))

    written = await _insert(session, rows)
    await session.commit()
    return written


async def build(
    session: AsyncSession, repo_id: int, snapshot: Snapshot, head_sha: str
) -> int:
    """Index a repository from scratch. Once, when it is first reviewed."""
    await session.execute(
        delete(SymbolReference).where(SymbolReference.repo_id == repo_id)
    )

    paths = [path for path in snapshot.files() if support_for(path) is not None]
    rows: list[dict] = []
    for path in paths:
        rows.extend(_rows(repo_id, path, snapshot))

    written = await _insert(session, rows)

    await session.execute(
        insert(RepositoryIndex)
        .values(repo_id=repo_id, head_sha=head_sha, files=len(paths), names=written)
        .on_conflict_do_update(
            index_elements=["repo_id"],
            set_={"head_sha": head_sha, "files": len(paths), "names": written},
        )
    )
    await session.commit()

    logger.info(
        "indexed %s: %s files, %s names", repo_id, len(paths), written
    )
    return written


async def indexed_at(session: AsyncSession, repo_id: int) -> str | None:
    return await session.scalar(
        select(RepositoryIndex.head_sha).where(RepositoryIndex.repo_id == repo_id)
    )


# Past this many rows a name is a word, not a reference, and there is no
# point carrying the rest back over the wire.
MAX_ROWS = 500


async def lookup(
    session: AsyncSession,
    repo_id: int,
    names: set[str],
    *,
    exclude_files: set[str] = frozenset(),
    changed_language: str | None = None,
    limit: int = 12,
) -> list[Candidate]:
    """Files elsewhere that mention these names, best first.

    Language ranks rather than excludes. A TypeScript file cannot call a
    Python function, so a shared name across that boundary is weaker
    evidence — but a response key and the property that reads it have
    nothing in common *except* the name, and excluding them outright is
    what makes that break invisible.
    """
    if not names:
        return []

    # One round trip. The database is remote, so a second query costs
    # more than counting a few hundred rows here does: splitting this
    # into a spread query and a fetch query doubled the latency of the
    # whole lookup on a small repository.
    rows = (await session.execute(
        select(SymbolReference)
        .where(
            SymbolReference.repo_id == repo_id,
            SymbolReference.name.in_(names),
            SymbolReference.file_path.notin_(exclude_files or {""}),
        )
        .limit(MAX_ROWS)
    )).scalars().all()

    everywhere: dict[str, set[str]] = {}
    for row in rows:
        everywhere.setdefault(row.name, set()).add(row.file_path)

    wanted = {n for n, files in everywhere.items() if len(files) <= TOO_WIDESPREAD}
    for name in set(everywhere) - wanted:
        logger.info(
            "'%s' is in %s files, too common to be a reference",
            name, len(everywhere[name]),
        )

    candidates = []
    for row in (r for r in rows if r.name in wanted):
        score = KIND_WEIGHT.get(row.kind, 0.5)
        if changed_language and row.language != changed_language:
            # Still worth reading, just less likely to be related.
            score *= 0.6
        # A name in two files is stronger evidence than one in twenty.
        score *= 1.0 - min(len(everywhere[row.name]) - 1, 20) / 40
        candidates.append(
            Candidate(
                file=row.file_path, line=row.line, kind=row.kind,
                language=row.language, name=row.name, score=round(score, 3),
            )
        )

    candidates.sort(key=lambda c: (-c.score, c.file, c.line))
    return candidates[:limit]
