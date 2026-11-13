from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from tree_sitter import Node, QueryCursor, Tree

from src.context.languages import LanguageSupport, same_language, support_for
from src.context.snapshot import Snapshot
from src.context.symbols import Symbol, check_parse_health, parse_source

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


@dataclass(frozen=True)
class ImportFacts:
    symbol_imported: bool = False
    module_imported: bool = False
    local_name: str | None = None

    # Without it, x.thing() cannot be tied back to "import * as x".
    module_alias: str | None = None


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


def import_targets_for(importing_file: str, defining_file: str) -> set[str]:
    """Every string an import in one file could use to name another."""
    support = support_for(defining_file)
    if support is None:
        return set()
    return support.import_targets(importing_file, defining_file)


def _text(node: Node) -> str:
    return node.text.decode("utf-8", "replace")


def find_candidates(
    snapshot: Snapshot, name: str, exclude: set[str], defined_in: str
) -> list[str]:
    needle = name.encode()
    found = [
        path
        for path in snapshot.files()
        if path not in exclude
        and same_language(path, defined_in)
        and needle in snapshot.read(path)
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


def analyse_imports(
    tree: Tree, defining_modules: set[str], symbol_name: str,
    support: LanguageSupport,
) -> ImportFacts:
    symbol_imported = False
    module_imported = False
    local_name: str | None = None
    module_alias: str | None = None

    for _, captures in QueryCursor(support.imports).matches(tree.root_node):
        if "module_only" in captures:
            module = support.normalise_import(_text(captures["module_only"][0]))
            if module in defining_modules:
                module_imported = True
                if "module_alias" in captures:
                    module_alias = _text(captures["module_alias"][0])
            continue

        if _text(captures["imported"][0]) != symbol_name:
            continue

        module = support.normalise_import(_text(captures["module"][0]))
        if module not in defining_modules:
            continue

        symbol_imported = True
        local_name = (
            _text(captures["alias"][0]) if "alias" in captures else symbol_name
        )

    return ImportFacts(symbol_imported, module_imported, local_name, module_alias)


def _call_sites(
    tree: Tree, wanted_name: str, support: LanguageSupport
) -> list[tuple[int, int]]:
    sites = []
    for _, captures in QueryCursor(support.calls).matches(tree.root_node):
        name_node = captures["name"][0]
        if _text(name_node) == wanted_name:
            sites.append((name_node.start_byte, name_node.start_point[0] + 1))
    return sites


def _attribute_call_sites(
    tree: Tree, wanted_name: str, support: LanguageSupport
) -> list[tuple[str, int]]:
    sites = []
    for _, captures in QueryCursor(support.attribute_calls).matches(tree.root_node):
        if _text(captures["name"][0]) == wanted_name:
            sites.append(
                (_text(captures["object"][0]), captures["call"][0].start_point[0] + 1)
            )
    return sites


def _bare_identifier_sites(
    tree: Tree, wanted_name: str, already_counted: set[int],
    support: LanguageSupport,
) -> list[int]:
    lines = []
    for _, captures in QueryCursor(support.identifiers).matches(tree.root_node):
        node = captures["name"][0]
        if _text(node) != wanted_name or node.start_byte in already_counted:
            continue
        if node.parent is not None and node.parent.type in support.import_node_types:
            continue
        lines.append(node.start_point[0] + 1)
    return lines


def verify_references(
    snapshot: Snapshot, path: str, symbol: Symbol
) -> list[Reference]:
    support = support_for(path)
    if support is None:
        return []

    tree = parse_source(snapshot.read(path), support)

    if not check_parse_health(tree).ok:
        logger.info("%s did not parse, treating the match as lexical only", path)
        return [
            Reference(
                path, 0, symbol, symbol.name, LEXICAL_MATCH, False,
                CONFIDENCE["lexical_only"],
            )
        ]

    defining_modules = import_targets_for(path, symbol.file)
    imports = analyse_imports(tree, defining_modules, symbol.name, support)
    local_name = imports.local_name or symbol.name

    references: list[Reference] = []
    counted_bytes: set[int] = set()

    for start_byte, line in _call_sites(tree, local_name, support):
        counted_bytes.add(start_byte)
        references.append(
            Reference(
                path, line, symbol, local_name, INBOUND_CALL,
                imports.symbol_imported,
                CONFIDENCE["call_with_import"] if imports.symbol_imported
                else CONFIDENCE["call_without_import"],
            )
        )

    for object_name, line in _attribute_call_sites(tree, symbol.name, support):
        through_module = imports.module_imported and (
            object_name in defining_modules or object_name == imports.module_alias
        )
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
        for line in _bare_identifier_sites(
            tree, local_name, counted_bytes, support
        ):
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

        candidates = find_candidates(
            snapshot, symbol.name, exclude=changed_files, defined_in=symbol.file
        )
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
