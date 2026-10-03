from __future__ import annotations

import logging
import tarfile
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx

from src.github.auth import API_HEADERS, GITHUB_API, get_installation_token

logger = logging.getLogger(__name__)

MAX_ARCHIVE_BYTES = 50 * 1024 * 1024
MAX_FILE_BYTES = 1024 * 1024
DOWNLOAD_TIMEOUT_SECONDS = 60.0

SOURCE_SUFFIXES = frozenset({".py"})

IGNORED_DIRECTORIES = frozenset({
    ".git",
    "node_modules",
    "vendor",
    "third_party",
    "dist",
    "build",
    "__pycache__",
    ".venv",
    "venv",
    "site-packages",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
})


class SnapshotTooLarge(Exception):
    pass


class SnapshotUnavailable(Exception):
    pass


class Snapshot:
    def __init__(self, root: Path, repo_full_name: str, sha: str) -> None:
        self.root = root
        self.repo_full_name = repo_full_name
        self.sha = sha
        self._cache: dict[str, bytes] = {}

    def files(self) -> list[str]:
        return sorted(
            str(path.relative_to(self.root))
            for path in self.root.rglob("*")
            if path.is_file()
        )

    def exists(self, path: str) -> bool:
        return (self.root / path).is_file()

    def read(self, path: str) -> bytes:
        if path not in self._cache:
            self._cache[path] = (self.root / path).read_bytes()
        return self._cache[path]

    def text(self, path: str) -> str:
        return self.read(path).decode("utf-8", "replace")

    def __len__(self) -> int:
        return len(self.files())

    def __repr__(self) -> str:
        return f"<Snapshot {self.repo_full_name}@{self.sha[:7]} {len(self)} files>"


def _wanted(name: str) -> bool:
    parts = Path(name).parts
    if any(part in IGNORED_DIRECTORIES for part in parts):
        return False
    return Path(name).suffix in SOURCE_SUFFIXES


def _safe_relative_path(name: str, prefix: str) -> Path | None:
    relative = name[len(prefix):].lstrip("/") if name.startswith(prefix) else name
    if not relative:
        return None

    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts:
        logger.warning("refusing a suspicious archive entry: %s", name)
        return None
    return candidate


async def _download(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    repo_full_name: str,
    sha: str,
    destination: Path,
) -> int:
    url = f"{GITHUB_API}/repos/{repo_full_name}/tarball/{sha}"
    downloaded = 0

    async with client.stream("GET", url, headers=headers, follow_redirects=True) as response:
        if response.status_code == 404:
            raise SnapshotUnavailable(f"{repo_full_name}@{sha[:7]} is not reachable")
        response.raise_for_status()

        with destination.open("wb") as handle:
            async for chunk in response.aiter_bytes():
                downloaded += len(chunk)
                if downloaded > MAX_ARCHIVE_BYTES:
                    raise SnapshotTooLarge(
                        f"{repo_full_name} exceeds {MAX_ARCHIVE_BYTES // 1024 // 1024} MB"
                    )
                handle.write(chunk)

    return downloaded


def _extract(archive: Path, into: Path) -> int:
    extracted = 0

    with tarfile.open(archive, mode="r:gz") as tar:
        members = tar.getmembers()
        if not members:
            raise SnapshotUnavailable("the archive was empty")

        prefix = members[0].name.split("/", 1)[0] + "/"

        for member in members:
            if not member.isfile() or not _wanted(member.name):
                continue

            if member.size > MAX_FILE_BYTES:
                logger.info("skipping %s, %s bytes", member.name, member.size)
                continue

            relative = _safe_relative_path(member.name, prefix)
            if relative is None:
                continue

            source = tar.extractfile(member)
            if source is None:
                continue

            target = into / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read())
            extracted += 1

    return extracted


@asynccontextmanager
async def open_snapshot(
    repo_full_name: str,
    sha: str,
    installation_id: int,
    *,
    client: httpx.AsyncClient | None = None,
) -> AsyncIterator[Snapshot]:
    we_made_the_client = client is None
    if we_made_the_client:
        client = httpx.AsyncClient(timeout=DOWNLOAD_TIMEOUT_SECONDS)

    try:
        token = await get_installation_token(installation_id, client=client)
        headers = {**API_HEADERS, "Authorization": f"token {token}"}

        with tempfile.TemporaryDirectory(prefix="snapshot-") as workspace:
            workspace_path = Path(workspace)
            archive = workspace_path / "repo.tar.gz"
            contents = workspace_path / "contents"
            contents.mkdir()

            size = await _download(client, headers, repo_full_name, sha, archive)
            count = _extract(archive, contents)
            archive.unlink()

            logger.info(
                "snapshot of %s@%s: %s KB downloaded, %s source files kept",
                repo_full_name,
                sha[:7],
                size // 1024,
                count,
            )

            yield Snapshot(contents, repo_full_name, sha)
    finally:
        if we_made_the_client:
            await client.aclose()
