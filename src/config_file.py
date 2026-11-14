"""Per-repository settings, read from the repository itself.

The product requirements have said since the beginning that low-severity
findings are hidden "unless explicitly requested", and there has been no
way to request them: one global set of thresholds applied to every
repository the App was installed on. TR-018 has read "not built"
alongside it.

The file lives in the repository under review, so the people whose pull
requests are being commented on own the settings, and the settings
arrive with the snapshot that is already being downloaded. No extra
request, and a branch can change them.

Every field is optional and a broken file is ignored with a warning: a
reviewer that refuses to review because somebody mistyped a threshold is
worse than one using defaults.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from src.filter.gate import MINIMUM_CONFIDENCE, POSTABLE_SEVERITIES
from src.github.review import MAX_COMMENTS_PER_REVIEW
from src.review.findings import CATEGORIES, SEVERITIES

logger = logging.getLogger(__name__)

# Checked in order; the first that exists wins.
LOCATIONS = (".pr-review.yml", ".pr-review.yaml", ".github/pr-review.yml")


@dataclass(frozen=True)
class RepoConfig:
    minimum_confidence: float = MINIMUM_CONFIDENCE
    severities: frozenset[str] = field(
        default_factory=lambda: frozenset(POSTABLE_SEVERITIES)
    )
    ignore_categories: frozenset[str] = frozenset()
    max_comments: int = MAX_COMMENTS_PER_REVIEW
    suggestions: bool = True

    # Settings the file asked for that we do not have, kept so the
    # summary can say so rather than silently doing nothing.
    unknown: tuple[str, ...] = ()


def _severities(raw) -> frozenset[str] | None:
    if not isinstance(raw, list):
        return None
    wanted = {str(item).strip().lower() for item in raw}
    valid = wanted & set(SEVERITIES)
    return frozenset(valid) if valid else None


def _categories(raw) -> frozenset[str]:
    if not isinstance(raw, list):
        return frozenset()
    return frozenset(
        str(item).strip().lower()
        for item in raw
        if str(item).strip().lower() in CATEGORIES
    )


def _confidence(raw) -> float | None:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if 0.0 <= value <= 1.0 else None


def parse(text: str) -> RepoConfig:
    """A config from the file's text, falling back to defaults field by field."""
    try:
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError as error:
        logger.warning("the repository config is not valid YAML: %s", error)
        return RepoConfig()

    if not isinstance(raw, dict):
        logger.warning("the repository config is not a mapping, ignoring it")
        return RepoConfig()

    known = {
        "minimum_confidence", "severities", "ignore_categories",
        "max_comments", "suggestions",
    }

    confidence = _confidence(raw.get("minimum_confidence"))
    severities = _severities(raw.get("severities"))

    try:
        cap = int(raw.get("max_comments", MAX_COMMENTS_PER_REVIEW))
    except (TypeError, ValueError):
        cap = MAX_COMMENTS_PER_REVIEW

    return RepoConfig(
        minimum_confidence=(
            confidence if confidence is not None else MINIMUM_CONFIDENCE
        ),
        severities=severities or frozenset(POSTABLE_SEVERITIES),
        ignore_categories=_categories(raw.get("ignore_categories")),
        max_comments=max(1, min(cap, MAX_COMMENTS_PER_REVIEW)),
        suggestions=bool(raw.get("suggestions", True)),
        unknown=tuple(sorted(set(raw) - known)),
    )


def load(root: Path | None) -> RepoConfig:
    """The config in this snapshot, or the defaults."""
    if root is None:
        return RepoConfig()

    for name in LOCATIONS:
        path = Path(root) / name
        try:
            if not path.is_file():
                continue
            config = parse(path.read_text(encoding="utf-8", errors="replace"))
        except OSError as error:
            logger.warning("could not read %s: %s", name, error)
            continue

        logger.info("using the repository's own settings from %s", name)
        if config.unknown:
            logger.warning(
                "%s asks for settings we do not have: %s",
                name, ", ".join(config.unknown),
            )
        return config

    return RepoConfig()
