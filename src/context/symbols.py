from __future__ import annotations

import logging
from dataclasses import dataclass

import tree_sitter_python
from tree_sitter import Language, Node, Parser, Tree

from src.context.snapshot import Snapshot
from src.diff.parse import FileDiff

logger = logging.getLogger(__name__)

PYTHON = Language(tree_sitter_python.language())
PARSER = Parser(PYTHON)

FUNCTION = "function_definition"
CLASS = "class_definition"
DEFINITION_TYPES = frozenset({FUNCTION, CLASS})

KIND_BY_TYPE = {FUNCTION: "function", CLASS: "class"}
MODULE_KIND = "module"
MODULE_NAME = "<module>"


@dataclass(frozen=True)
class ParseHealth:
    ok: bool
    error_nodes: int = 0
    missing_nodes: int = 0


@dataclass(frozen=True)
class Symbol:
    name: str
    qualified_name: str
    kind: str
    file: str
    start_line: int
    end_line: int

    @property
    def is_module_level(self) -> bool:
        return self.kind == MODULE_KIND

    def __str__(self) -> str:
        return f"{self.file}:{self.start_line} {self.kind} {self.qualified_name}"


def parse_source(source: bytes) -> Tree:
    return PARSER.parse(source)


def _walk(node: Node):
    yield node
    for child in node.children:
        yield from _walk(child)


def check_parse_health(tree: Tree) -> ParseHealth:
    if not tree.root_node.has_error:
        return ParseHealth(ok=True)

    errors = 0
    missing = 0
    for node in _walk(tree.root_node):
        if node.type == "ERROR":
            errors += 1
        if node.is_missing:
            missing += 1

    return ParseHealth(ok=False, error_nodes=errors, missing_nodes=missing)


def _first_code_column(line: str) -> int:
    return len(line) - len(line.lstrip()) if line.strip() else 0


def _definition_name(node: Node) -> str:
    name_node = node.child_by_field_name("name")
    return name_node.text.decode("utf-8", "replace") if name_node else "<anonymous>"


def _enclosing_definitions(node: Node) -> list[Node]:
    chain: list[Node] = []
    current: Node | None = node
    while current is not None:
        if current.type in DEFINITION_TYPES:
            chain.append(current)
        current = current.parent
    return chain


def symbol_at_line(
    tree: Tree, source_lines: list[str], file: str, line_number: int
) -> Symbol | None:
    row = line_number - 1
    if row < 0 or row >= len(source_lines):
        return None

    column = _first_code_column(source_lines[row])
    node = tree.root_node.named_descendant_for_point_range((row, column), (row, column))
    if node is None:
        return None

    chain = _enclosing_definitions(node)

    if not chain:
        return Symbol(
            name=MODULE_NAME,
            qualified_name=MODULE_NAME,
            kind=MODULE_KIND,
            file=file,
            start_line=1,
            end_line=len(source_lines),
        )

    innermost = chain[0]
    names = [_definition_name(definition) for definition in reversed(chain)]

    return Symbol(
        name=names[-1],
        qualified_name=".".join(names),
        kind=KIND_BY_TYPE[innermost.type],
        file=file,
        start_line=innermost.start_point[0] + 1,
        end_line=innermost.end_point[0] + 1,
    )


def symbols_in_file(
    snapshot: Snapshot, file: str, line_numbers: list[int]
) -> tuple[list[Symbol], ParseHealth]:
    source = snapshot.read(file)
    tree = parse_source(source)
    health = check_parse_health(tree)

    if not health.ok:
        logger.warning(
            "%s did not parse cleanly: %s error nodes, %s missing",
            file,
            health.error_nodes,
            health.missing_nodes,
        )
        return [], health

    source_lines = source.decode("utf-8", "replace").splitlines()
    found: dict[tuple[str, int], Symbol] = {}

    for line_number in line_numbers:
        symbol = symbol_at_line(tree, source_lines, file, line_number)
        if symbol is not None:
            found[(symbol.qualified_name, symbol.start_line)] = symbol

    return list(found.values()), health


def changed_symbols(
    snapshot: Snapshot, diffs: list[FileDiff]
) -> tuple[list[Symbol], dict[str, str]]:
    symbols: list[Symbol] = []
    problems: dict[str, str] = {}

    for diff in diffs:
        if not diff.parsed:
            problems[diff.filename] = f"the diff could not be read: {diff.parse_error}"
            continue

        if not snapshot.exists(diff.filename):
            problems[diff.filename] = "not in the snapshot"
            continue

        found, health = symbols_in_file(
            snapshot, diff.filename, diff.added_line_numbers
        )

        if not health.ok:
            problems[diff.filename] = (
                f"the file did not parse: {health.error_nodes} error nodes"
            )
            continue

        symbols.extend(found)

    return symbols, problems
