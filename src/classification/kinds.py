from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

API = "api"
SCHEMA = "schema"
DEPENDENCY = "dependency"
CONFIG = "config"
LOGIC = "logic"
TEST = "test"
DOCS = "docs"

CHANGE_KINDS = (API, SCHEMA, DEPENDENCY, CONFIG, LOGIC, TEST, DOCS)

RULE_BASED = "rule"
MODEL_BASED = "model"
FALLBACK = "fallback"

DEPENDENCY_FILES = frozenset({
    "requirements.txt",
    "requirements-dev.txt",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "Pipfile",
    "package.json",
    "go.mod",
    "Cargo.toml",
    "Gemfile",
    "composer.json",
})

CONFIG_FILES = frozenset({
    "config.py",
    "settings.py",
    "alembic.ini",
    "pytest.ini",
    "tox.ini",
    "Dockerfile",
    "docker-compose.yml",
    "docker-compose.yaml",
})

MIGRATION_MARKERS = ("alembic/versions/", "migrations/", "/migrate/")
DOC_SUFFIXES = (".md", ".rst", ".txt", ".adoc")
CONFIG_SUFFIXES = (".ini", ".cfg", ".toml", ".yaml", ".yml", ".env")

TEST_PATTERN = re.compile(r"(^|/)(tests?)(/|$)|(^|/)test_[^/]+$|_test\.[a-z]+$")


@dataclass(frozen=True)
class Classification:
    file: str
    kind: str
    source: str
    reason: str = ""

    @property
    def needs_context(self) -> bool:
        return self.kind in (API, SCHEMA, LOGIC)


def classify_by_rules(path: str) -> Classification | None:
    name = Path(path).name

    if TEST_PATTERN.search(path):
        return Classification(path, TEST, RULE_BASED, "the path says it is a test")

    if any(marker in f"/{path}" for marker in MIGRATION_MARKERS):
        return Classification(path, SCHEMA, RULE_BASED, "it lives with the migrations")

    if name in DEPENDENCY_FILES:
        return Classification(path, DEPENDENCY, RULE_BASED, "it declares dependencies")

    if path.endswith(DOC_SUFFIXES):
        return Classification(path, DOCS, RULE_BASED, "it is documentation")

    if name in CONFIG_FILES or path.endswith(CONFIG_SUFFIXES):
        return Classification(path, CONFIG, RULE_BASED, "it is configuration")

    return None
