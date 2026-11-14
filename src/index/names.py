"""Every name a file contains, and where.

The reviewer's existing retrieval asks "does this file use that symbol",
which it answers by reading every file in the repository looking for the
name — once per changed symbol, every review, then throwing the answer
away. On a repository of any size that is the slowest thing in the
pipeline, and it is recomputed from scratch each time.

This asks the smaller question instead: what names does this file contain
at all. Stored once, it answers the first question with a lookup.

Two deliberate choices about what gets indexed:

**Dictionary keys and property names, not only symbols.** The defect this
exists to catch is a key renamed in a Python response and still read as a
property in TypeScript. There is no call, no import and no symbol to
resolve between those two — only a word in common. Indexing identifiers
alone would miss it, which is what happens today.

**Not every string literal.** A naive index of every string in the
repository would bury the signal. Only strings in positions that carry a
data contract are kept: dictionary keys, subscripts, keyword arguments,
object literal keys and TypeScript property signatures.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from tree_sitter import QueryCursor

from src.context.languages import LanguageSupport, support_for
from src.context.snapshot import Snapshot

logger = logging.getLogger(__name__)

# A name shorter than this matches everything and means nothing.
MINIMUM_LENGTH = 3

# Words that appear in every file in every language. Indexing them costs
# storage and returns the whole repository as a candidate.
TOO_COMMON = frozenset({
    "self", "cls", "this", "props", "state", "data", "value", "item",
    "items", "result", "results", "args", "kwargs", "error", "err",
    "name", "type", "key", "id", "get", "set", "map", "list", "dict",
    "str", "int", "len", "print", "return", "true", "false", "null",
    "none", "object", "string", "number", "boolean", "array", "index",
})

# What a capture means. The order is the ranking order: a definition is
# worth more than a mention.
KIND_WEIGHT = {
    "definition": 1.0,
    "key": 0.9,
    "property": 0.8,
    "call": 0.7,
    "argument": 0.6,
}


@dataclass(frozen=True)
class Name:
    text: str
    line: int
    kind: str

    def __str__(self) -> str:
        return f"{self.text} ({self.kind}, line {self.line})"


def worth_indexing(text: str) -> bool:
    return (
        len(text) >= MINIMUM_LENGTH
        and text.lower() not in TOO_COMMON
        and not text.startswith("__")
    )


def names_in(source: bytes, support: LanguageSupport) -> list[Name]:
    """Every indexable name in one file, deduplicated by name and line."""
    try:
        tree = support.parser.parse(source)
    except (ValueError, RecursionError) as error:
        logger.warning("could not parse a file for the index: %s", error)
        return []

    # A keyword argument is a calling convention, not a data key, and
    # conflating them hides renames: `rollout_percent=body.rollout_percent`
    # elsewhere in the file made a removed dictionary key of the same name
    # look like it was still there.
    seen: set[tuple[str, int, str]] = set()
    for kind, nodes in QueryCursor(support.indexable).captures(tree.root_node).items():
        for node in nodes:
            text = node.text.decode("utf-8", "replace") if node.text else ""
            if worth_indexing(text):
                seen.add((text, node.start_point[0] + 1, kind))

    return [Name(text, line, kind) for text, line, kind in sorted(seen)]


def names_in_file(snapshot: Snapshot, path: str) -> list[Name]:
    support = support_for(path)
    if support is None:
        return []
    try:
        return names_in(snapshot.read(path), support)
    except (OSError, KeyError):
        return []


def removed_names(before: list[Name], after: list[Name]) -> set[str]:
    """Names whose role this change took away.

    The question that finds a rename, and it has to be asked about the
    role rather than the bare word. Renaming a response key from
    `rollout_percent` to `rollout` leaves `f.rollout_percent` sitting on
    the same line as a property access, so the word survives the change
    and a plain text comparison reports nothing.

    What disappeared is the *key*. Comparing (name, role) pairs catches
    that, and the lookup afterwards is on the bare word, because the far
    end of the break calls it whatever its own language calls it.
    """
    lost = {(n.text, n.kind) for n in before} - {(n.text, n.kind) for n in after}
    return {text for text, _ in lost}


def before_text(diff, after: str) -> str:
    """The file as it was, rebuilt from the version we have and the diff.

    Parsing the removed lines on their own does not work. A removed line
    like `"rollout_percent": flag.rollout_percent,` is a fragment: out of
    its dictionary it is not a dictionary pair, so the query that finds
    keys never matches and the rename that caused all this goes unseen.

    The whole file parses, so rebuild the whole file. Everything outside
    a hunk is unchanged, and inside a hunk the old version is the lines
    that were not added.
    """
    lines = after.splitlines()
    rebuilt: list[str] = []
    cursor = 0  # how far through the new file we have copied, 0-based

    for hunk in sorted(diff.hunks, key=lambda h: h.new_start):
        start = max(hunk.new_start - 1, 0)
        rebuilt.extend(lines[cursor:start])
        rebuilt.extend(
            line.content for line in hunk.lines if not line.is_added
        )
        cursor = start + hunk.new_length

    rebuilt.extend(lines[cursor:])
    return "\n".join(rebuilt) + "\n"


def names_in_diff(diff, after: str, support: LanguageSupport) -> set[str]:
    """Names whose role this change took away, from one changed file."""
    if not any(hunk.removed for hunk in diff.hunks):
        return set()

    return removed_names(
        names_in(before_text(diff, after).encode(), support),
        names_in(after.encode(), support),
    )
