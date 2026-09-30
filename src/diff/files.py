from __future__ import annotations

import logging
from dataclasses import dataclass

from src.github.client import GitHubClient

logger = logging.getLogger(__name__)

GITHUB_FILE_LIMIT = 3000

STATUS_ADDED = "added"
STATUS_REMOVED = "removed"
STATUS_MODIFIED = "modified"
STATUS_RENAMED = "renamed"


@dataclass(frozen=True)
class ChangedFile:
    filename: str
    status: str
    additions: int
    deletions: int
    changes: int
    patch: str | None = None
    previous_filename: str | None = None

    @property
    def has_patch(self) -> bool:
        return bool(self.patch)

    @property
    def is_deleted(self) -> bool:
        return self.status == STATUS_REMOVED

    @property
    def is_renamed(self) -> bool:
        return self.status == STATUS_RENAMED

    @classmethod
    def from_api(cls, data: dict) -> "ChangedFile":
        return cls(
            filename=data["filename"],
            status=data["status"],
            additions=data.get("additions", 0),
            deletions=data.get("deletions", 0),
            changes=data.get("changes", 0),
            patch=data.get("patch"),
            previous_filename=data.get("previous_filename"),
        )


@dataclass(frozen=True)
class ChangedFileSet:
    files: list[ChangedFile]
    truncated: bool = False

    def __len__(self) -> int:
        return len(self.files)

    def __iter__(self):
        return iter(self.files)

    @property
    def reviewable(self) -> list[ChangedFile]:
        return [f for f in self.files if f.has_patch and not f.is_deleted]

    @property
    def without_patch(self) -> list[ChangedFile]:
        return [f for f in self.files if not f.has_patch and not f.is_deleted]

    @property
    def total_changes(self) -> int:
        return sum(f.changes for f in self.files)


async def fetch_changed_files(
    github: GitHubClient, repo_full_name: str, pr_number: int
) -> ChangedFileSet:
    raw = await github.get_all_pages(
        f"/repos/{repo_full_name}/pulls/{pr_number}/files"
    )

    files = [ChangedFile.from_api(item) for item in raw]
    truncated = len(files) >= GITHUB_FILE_LIMIT

    if truncated:
        logger.warning(
            "%s#%s reports %s files, which is GitHub's limit; the change may be "
            "larger than we can see",
            repo_full_name,
            pr_number,
            len(files),
        )

    skipped = len([f for f in files if not f.has_patch and not f.is_deleted])
    if skipped:
        logger.info(
            "%s#%s: %s of %s files have no diff and cannot be reviewed",
            repo_full_name,
            pr_number,
            skipped,
            len(files),
        )

    return ChangedFileSet(files=files, truncated=truncated)
