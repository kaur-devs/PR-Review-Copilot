"""Storing the index, keeping it current, and ranking what it returns."""

import pathlib

import pytest
from sqlalchemy import func, select

from src.context.snapshot import Snapshot
from src.db.models import SymbolReference
from src.db.queries import ensure_repo
from src.db.session import get_session_factory
from src.index import store
from src.webhooks.events import PullRequestEvent

EVENT = PullRequestEvent(
    delivery_id="d-1", action="opened", installation_id=42, repo_id=987654,
    repo_full_name="kaur-devs/sandbox", pr_number=3,
    head_sha="a" * 40, base_sha="b" * 40,
)

BACKEND = 'def as_json(flag):\n    return {"rollout_percent": flag.enabled}\n'
TYPES = "export interface Flag { rollout_percent: number; }\n"
ROW = "export default function Row({flag}) { return flag.rollout_percent; }\n"
UNRELATED = "def unrelated():\n    return 1\n"


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "backend").mkdir()
    (tmp_path / "frontend").mkdir()
    (tmp_path / "backend" / "flags.py").write_text(BACKEND)
    (tmp_path / "backend" / "other.py").write_text(UNRELATED)
    (tmp_path / "frontend" / "types.ts").write_text(TYPES)
    (tmp_path / "frontend" / "Row.tsx").write_text(ROW)
    return Snapshot(tmp_path, "kaur-devs/sandbox", "a" * 40)


async def a_repo_id(session):
    return await ensure_repo(session, EVENT)


async def test_the_index_crosses_the_language_boundary(repo):
    """A Python file cannot call a TypeScript function, but it can hand
    it a dictionary. Excluding other languages outright is what makes a
    renamed response key invisible."""
    async with get_session_factory()() as session:
        repo_id = await a_repo_id(session)
        await store.build(session, repo_id, repo, "a" * 40)

        found = await store.lookup(
            session, repo_id, {"rollout_percent"},
            exclude_files={"backend/flags.py"}, changed_language="python",
        )

    assert {c.file for c in found} == {"frontend/types.ts", "frontend/Row.tsx"}


async def test_language_ranks_rather_than_excludes(repo):
    """Same-language evidence is stronger, so it sorts first — but the
    cross-language candidate is still returned."""
    async with get_session_factory()() as session:
        repo_id = await a_repo_id(session)
        await store.build(session, repo_id, repo, "a" * 40)

        same = await store.lookup(session, repo_id, {"rollout_percent"},
                                  changed_language="python")
        cross = [c for c in same if c.language != "python"]

    assert cross, "a cross-language candidate must survive"
    assert same[0].language == "python", "same-language evidence ranks first"


async def test_a_definition_outranks_a_mention(repo):
    async with get_session_factory()() as session:
        repo_id = await a_repo_id(session)
        await store.build(session, repo_id, repo, "a" * 40)

        found = await store.lookup(session, repo_id, {"as_json", "rollout_percent"})

    by_kind = {c.kind: c.score for c in found}
    assert by_kind["definition"] > by_kind.get("property", 0)


async def test_changing_one_file_replaces_only_its_rows(repo):
    """Delete and re-insert rather than reconcile. Nothing was resolved,
    so there are no relationships elsewhere to repair — the reason this
    is cheaper to keep current than a resolved graph."""
    async with get_session_factory()() as session:
        repo_id = await a_repo_id(session)
        await store.build(session, repo_id, repo, "a" * 40)
        before = await session.scalar(
            select(func.count()).select_from(SymbolReference)
            .where(SymbolReference.file_path == "frontend/types.ts")
        )

        # A Snapshot is one immutable commit and caches what it reads, so
        # a later commit is a new Snapshot — which is what the pipeline
        # does on every review.
        (repo.root / "frontend" / "types.ts").write_text(
            "export interface Flag { rollout: number; }\n"
        )
        later = Snapshot(repo.root, "kaur-devs/sandbox", "b" * 40)
        await store.reindex_files(session, repo_id, later, ["frontend/types.ts"])

        names = set((await session.execute(
            select(SymbolReference.name)
            .where(SymbolReference.file_path == "frontend/types.ts")
        )).scalars())
        untouched = await session.scalar(
            select(func.count()).select_from(SymbolReference)
            .where(SymbolReference.file_path == "backend/flags.py")
        )

    assert before > 0
    assert "rollout_percent" not in names and "rollout" in names
    assert untouched > 0, "other files are left alone"


async def test_a_name_in_everything_is_not_a_reference(repo, monkeypatch):
    monkeypatch.setattr(store, "TOO_WIDESPREAD", 1)
    async with get_session_factory()() as session:
        repo_id = await a_repo_id(session)
        await store.build(session, repo_id, repo, "a" * 40)

        found = await store.lookup(session, repo_id, {"rollout_percent"})

    assert found == [], "three files is too widespread once the bar is one"


async def test_the_indexed_commit_is_recorded(repo):
    async with get_session_factory()() as session:
        repo_id = await a_repo_id(session)
        assert await store.indexed_at(session, repo_id) is None

        await store.build(session, repo_id, repo, "c0ffee" + "0" * 34)

        assert await store.indexed_at(session, repo_id) == "c0ffee" + "0" * 34
