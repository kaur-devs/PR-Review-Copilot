from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from tree_sitter import Node, Query, QueryCursor, Tree

from src.context.snapshot import Snapshot
from src.context.symbols import PYTHON, Symbol, check_parse_health, parse_source

logger = logging.getLogger(__name__)

INBOUND_CALL = "inbound_call"
INBOUND_REFERENCE = "inbound_reference"
ATTRIBUTE_CALL = "attribute_call"
LEXICAL_MATCH = "lexical_match"

CONFIDENCE = {
    "call_with_import": 0.9,
    "call_through_module": 0.9,
    "reference_with_import": 0.7,
    "call_without_import": 0.5,
    "attribute_elsewhere": 0.25,
    "lexical_only": 0.1,
}

MAX_CANDIDATES_PER_SYMBOL = 40

CALLS = Query(PYTHON, "(call function: (identifier) @name) @call")

ATTRIBUTE_CALLS = Query(
    PYTHON,
    "(call function: (attribute object: (identifier) @object"
    "                          attribute: (identifier) @name)) @call",
)

IDENTIFIERS = Query(PYTHON, "(identifier) @name")

IMPORTS = Query(
    PYTHON,
    """
    (import_from_statement
      module_name: (_) @module
      name: (dotted_name) @imported)

    (import_from_statement
      module_name: (_) @module
      name: (aliased_import
              name: (dotted_name) @imported
              alias: (identifier) @alias))

    (import_statement name: (dotted_name) @module_only)
    """,
)


@dataclass(frozen=True)
class ImportFacts:
    symbol_imported: bool = False
    module_imported: bool = False
    local_name: str | None = None


@dataclass(frozen=True)
class Reference:
    file: str
    line: int
    symbol: Symbol
    local_name: str
    relation: str
    import_confirmed: bool
    confidence: float

    @property
    def is_strong(self) -> bool:
        return self.confidence >= 0.5

    def __str__(self) -> str:
        return (
            f"{self.file}:{self.line} {self.relation} "
            f"{self.local_name} ({self.confidence})"
        )


def module_names_for(path: str) -> set[str]:
    parts = Path(path).with_suffix("").parts
    return {".".join(parts[index:]) for index in range(len(parts))}


def _text(node: Node) -> str:
    return node.text.decode("utf-8", "replace")


def find_candidates(
    snapshot: Snapshot, name: str, exclude: set[str]
) -> list[str]:
    needle = name.encode()
    found = [
        path
        for path in snapshot.files()
        if path not in exclude and needle in snapshot.read(path)
    ]

    if len(found) > MAX_CANDIDATES_PER_SYMBOL:
        logger.info(
            "'%s' appears in %s files, keeping the first %s",
            name,
            len(found),
            MAX_CANDIDATES_PER_SYMBOL,
        )
        return found[:MAX_CANDIDATES_PER_SYMBOL]

    return found


def analyse_imports(tree: Tree, defining_modules: set[str], symbol_name: str) -> ImportFacts:
    symbol_imported = False
    module_imported = False
    local_name: str | None = None

    for _, captures in QueryCursor(IMPORTS).matches(tree.root_node):
        if "module_only" in captures:
            if _text(captures["module_only"][0]) in defining_modules:
                module_imported = True
            continue

        if _text(captures["imported"][0]) != symbol_name:
            continue

        module = _text(captures["module"][0]).lstrip(".")
        if module not in defining_modules:
            continue

        symbol_imported = True
        local_name = (
            _text(captures["alias"][0]) if "alias" in captures else symbol_name
        )

    return ImportFacts(symbol_imported, module_imported, local_name)


def _call_sites(tree: Tree, wanted_name: str) -> list[tuple[int, int]]:
    sites = []
    for _, captures in QueryCursor(CALLS).matches(tree.root_node):
        name_node = captures["name"][0]
        if _text(name_node) == wanted_name:
            sites.append((name_node.start_byte, name_node.start_point[0] + 1))
    return sites


def _attribute_call_sites(tree: Tree, wanted_name: str) -> list[tuple[str, int]]:
    sites = []
    for _, captures in QueryCursor(ATTRIBUTE_CALLS).matches(tree.root_node):
        if _text(captures["name"][0]) == wanted_name:
            sites.append(
                (_text(captures["object"][0]), captures["call"][0].start_point[0] + 1)
            )
    return sites


def _bare_identifier_sites(
    tree: Tree, wanted_name: str, already_counted: set[int]
) -> list[int]:
    lines = []
    for _, captures in QueryCursor(IDENTIFIERS).matches(tree.root_node):
        node = captures["name"][0]
        if _text(node) != wanted_name or node.start_byte in already_counted:
            continue
        if node.parent is not None and node.parent.type in (
            "import_from_statement",
            "dotted_name",
            "aliased_import",
        ):
            continue
        lines.append(node.start_point[0] + 1)
    return lines


def verify_references(
    snapshot: Snapshot, path: str, symbol: Symbol
) -> list[Reference]:
    tree = parse_source(snapshot.read(path))

    if not check_parse_health(tree).ok:
        logger.info("%s did not parse, treating the match as lexical only", path)
        return [
            Reference(
                path, 0, symbol, symbol.name, LEXICAL_MATCH, False,
                CONFIDENCE["lexical_only"],
            )
        ]

    defining_modules = module_names_for(symbol.file)
    imports = analyse_imports(tree, defining_modules, symbol.name)
    local_name = imports.local_name or symbol.name

    references: list[Reference] = []
    counted_bytes: set[int] = set()

    for start_byte, line in _call_sites(tree, local_name):
        counted_bytes.add(start_byte)
        references.append(
            Reference(
                path, line, symbol, local_name, INBOUND_CALL,
                imports.symbol_imported,
                CONFIDENCE["call_with_import"] if imports.symbol_imported
                else CONFIDENCE["call_without_import"],
            )
        )

    for object_name, line in _attribute_call_sites(tree, symbol.name):
        through_module = imports.module_imported and object_name in defining_modules
        references.append(
            Reference(
                path, line, symbol, f"{object_name}.{symbol.name}",
                INBOUND_CALL if through_module else ATTRIBUTE_CALL,
                through_module,
                CONFIDENCE["call_through_module"] if through_module
                else CONFIDENCE["attribute_elsewhere"],
            )
        )

    if imports.symbol_imported:
        for line in _bare_identifier_sites(tree, local_name, counted_bytes):
            references.append(
                Reference(
                    path, line, symbol, local_name, INBOUND_REFERENCE, True,
                    CONFIDENCE["reference_with_import"],
                )
            )

    if not references:
        references.append(
            Reference(
                path, 0, symbol, symbol.name, LEXICAL_MATCH, False,
                CONFIDENCE["lexical_only"],
            )
        )

    return references


def find_references(
    snapshot: Snapshot, symbols: list[Symbol], changed_files: set[str]
) -> list[Reference]:
    references: list[Reference] = []

    for symbol in symbols:
        if symbol.is_module_level:
            continue

        candidates = find_candidates(snapshot, symbol.name, exclude=changed_files)
        if not candidates:
            continue

        for path in candidates:
            references.extend(verify_references(snapshot, path, symbol))

    logger.info(
        "%s references found for %s symbols (%s strong)",
        len(references),
        len(symbols),
        len([r for r in references if r.is_strong]),
    )
    return references
