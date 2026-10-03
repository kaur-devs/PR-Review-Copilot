import io
import tarfile
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from src.context import snapshot as snap
from src.context.snapshot import (
    SnapshotTooLarge,
    SnapshotUnavailable,
    open_snapshot,
)
from src.github import auth

PREFIX = "kaur-devs-codebase-chat-1557103"

DEFAULT_ENTRIES = {
    "backend/app/models.py": b"def get_user(user_id):\n    return None\n",
    "backend/app/service.py": b"from models import get_user\n",
    "frontend/src/App.jsx": b"export default App\n",
    "README.md": b"# readme\n",
    "node_modules/left-pad/index.js": b"module.exports = 1\n",
    ".git/config": b"[core]\n",
    "__pycache__/models.cpython-314.pyc": b"\x00\x01",
}


@pytest.fixture(autouse=True)
def forget_cached_tokens():
    auth.clear_token_cache()
    yield
    auth.clear_token_cache()


def build_tarball(entries=None, *, prefix=PREFIX, padding=0) -> bytes:
    entries = DEFAULT_ENTRIES if entries is None else entries
    buffer = io.BytesIO()

    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        root = tarfile.TarInfo(prefix)
        root.type = tarfile.DIRTYPE
        tar.addfile(root)

        for path, content in entries.items():
            if padding:
                content = content + b"x" * padding
            info = tarfile.TarInfo(f"{prefix}/{path}")
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))

    return buffer.getvalue()


def fake_github(archive: bytes | None = None, *, status: int = 200):
    expires_at = datetime.now(timezone.utc) + timedelta(hours=1)

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/access_tokens"):
            return httpx.Response(
                200,
                json={
                    "token": "ghs_test",
                    "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
                },
            )
        if status != 200:
            return httpx.Response(status, json={"message": "no"})
        return httpx.Response(200, content=archive)

    return httpx.AsyncClient(transport=httpx.MockTransport(handle))


async def test_source_files_are_extracted():
    async with open_snapshot("o/r", "abc123", 42, client=fake_github(build_tarball())) as s:
        assert "backend/app/models.py" in s.files()
        assert "backend/app/service.py" in s.files()


async def test_the_archive_prefix_is_stripped():
    async with open_snapshot("o/r", "abc123", 42, client=fake_github(build_tarball())) as s:
        assert all(not path.startswith(PREFIX) for path in s.files())


async def test_only_python_files_are_kept():
    async with open_snapshot("o/r", "abc123", 42, client=fake_github(build_tarball())) as s:
        assert all(path.endswith(".py") for path in s.files())
        assert "README.md" not in s.files()
        assert "frontend/src/App.jsx" not in s.files()


async def test_vendored_and_generated_directories_are_skipped():
    async with open_snapshot("o/r", "abc123", 42, client=fake_github(build_tarball())) as s:
        files = s.files()
        assert not any("node_modules" in path for path in files)
        assert not any(".git" in path for path in files)
        assert not any("__pycache__" in path for path in files)


async def test_file_contents_come_back():
    async with open_snapshot("o/r", "abc123", 42, client=fake_github(build_tarball())) as s:
        assert b"def get_user" in s.read("backend/app/models.py")
        assert "from models import get_user" in s.text("backend/app/service.py")


async def test_reading_the_same_file_twice_uses_the_cache():
    async with open_snapshot("o/r", "abc123", 42, client=fake_github(build_tarball())) as s:
        first = s.read("backend/app/models.py")
        assert s.read("backend/app/models.py") is first


async def test_exists_answers_both_ways():
    async with open_snapshot("o/r", "abc123", 42, client=fake_github(build_tarball())) as s:
        assert s.exists("backend/app/models.py")
        assert not s.exists("backend/app/nothing.py")


async def test_everything_is_removed_afterwards():
    async with open_snapshot("o/r", "abc123", 42, client=fake_github(build_tarball())) as s:
        root = s.root
        assert root.exists()

    assert not root.exists()


async def test_an_unreachable_commit_is_reported():
    with pytest.raises(SnapshotUnavailable):
        async with open_snapshot("o/r", "missing", 42, client=fake_github(status=404)):
            pass


async def test_an_oversized_archive_is_refused(monkeypatch):
    monkeypatch.setattr(snap, "MAX_ARCHIVE_BYTES", 500)
    big = build_tarball(padding=5000)

    with pytest.raises(SnapshotTooLarge):
        async with open_snapshot("o/r", "abc123", 42, client=fake_github(big)):
            pass


async def test_an_oversized_single_file_is_skipped(monkeypatch):
    monkeypatch.setattr(snap, "MAX_FILE_BYTES", 50)
    archive = build_tarball({
        "small.py": b"x = 1\n",
        "huge.py": b"y = 2\n" + b"#" * 500,
    })

    async with open_snapshot("o/r", "abc123", 42, client=fake_github(archive)) as s:
        assert s.files() == ["small.py"]


async def test_an_entry_escaping_the_folder_is_refused():
    archive = build_tarball({"../../escaped.py": b"bad = 1\n"})

    async with open_snapshot("o/r", "abc123", 42, client=fake_github(archive)) as s:
        assert s.files() == []


async def test_an_empty_archive_is_reported():
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz"):
        pass

    with pytest.raises(SnapshotUnavailable):
        async with open_snapshot("o/r", "abc123", 42, client=fake_github(buffer.getvalue())):
            pass
