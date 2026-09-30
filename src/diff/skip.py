from __future__ import annotations

from dataclasses import dataclass

from src.diff.files import ChangedFile

MAX_CHANGED_LINES_PER_FILE = 500

LOCKFILES = frozenset({
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "poetry.lock",
    "Pipfile.lock",
    "Gemfile.lock",
    "Cargo.lock",
    "composer.lock",
    "go.sum",
})

VENDORED_DIRECTORIES = (
    "node_modules/",
    "vendor/",
    "third_party/",
    "thirdparty/",
    "dist/",
    "build/",
    ".venv/",
    "site-packages/",
    "__snapshots__/",
)

GENERATED_SUFFIXES = (
    ".min.js",
    ".min.css",
    ".map",
    ".snap",
    "_pb2.py",
    "_pb2_grpc.py",
    ".pb.go",
    ".generated.ts",
    ".lock",
)


@dataclass(frozen=True)
class SkipDecision:
    skip: bool
    reason: str | None = None


def decide(changed: ChangedFile) -> SkipDecision:
    path = changed.filename
    name = path.rsplit("/", 1)[-1]

    if changed.is_deleted:
        return SkipDecision(True, "the file was deleted")

    if not changed.has_patch:
        return SkipDecision(True, "GitHub sent no diff for it")

    if name in LOCKFILES:
        return SkipDecision(True, "it is a lockfile")

    if any(part in path for part in VENDORED_DIRECTORIES):
        return SkipDecision(True, "it is vendored or generated output")

    if any(path.endswith(suffix) for suffix in GENERATED_SUFFIXES):
        return SkipDecision(True, "it looks machine-generated")

    if changed.changes > MAX_CHANGED_LINES_PER_FILE:
        return SkipDecision(
            True, f"it changes {changed.changes} lines, over our limit of {MAX_CHANGED_LINES_PER_FILE}"
        )

    return SkipDecision(False)


def worth_reviewing(files: list[ChangedFile]) -> tuple[list[ChangedFile], dict[str, str]]:
    keep: list[ChangedFile] = []
    skipped: dict[str, str] = {}

    for changed in files:
        decision = decide(changed)
        if decision.skip:
            skipped[changed.filename] = decision.reason or "unknown"
        else:
            keep.append(changed)

    return keep, skipped
