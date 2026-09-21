"""Connected-file context: end-to-end development spike.

Runs the whole connected-file context pipeline locally, with no GitHub calls
and no LLM calls, so the flow can be watched stage by stage:

    parsed diff -> snapshot -> changed symbols -> candidates -> verified
    references -> context bundle

This is throwaway experimental code. The real module lives in src/context/
and should be written against these ideas, not copied from this file.

Run:  .venv/bin/python spikes/context_spike.py
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import tree_sitter_python
from tree_sitter import Language, Node, Parser, Query, QueryCursor
from unidiff import PatchSet

PY_LANGUAGE = Language(tree_sitter_python.language())
PARSER = Parser(PY_LANGUAGE)

DEFINITION_TYPES = {"function_definition", "class_definition"}
CONFIDENCE_THRESHOLD = 0.5
CHAR_BUDGET = 4000
SNIPPET_PADDING = 2


# --------------------------------------------------------------------------
# Queries. Each one answers a single syntactic question.
# --------------------------------------------------------------------------

Q_CALLS = Query(PY_LANGUAGE, """
(call function: (identifier) @name) @call
""")

Q_ATTRIBUTE_CALLS = Query(PY_LANGUAGE, """
(call function: (attribute object: (identifier) @object
                          attribute: (identifier) @name)) @call
""")

Q_IMPORTS = Query(PY_LANGUAGE, """
(import_from_statement
  module_name: (_) @module
  name: (dotted_name) @imported)

(import_from_statement
  module_name: (_) @module
  name: (aliased_import
          name: (dotted_name) @imported
          alias: (identifier) @alias))

(import_statement name: (dotted_name) @module_only)
""")

Q_DEFINITIONS = Query(PY_LANGUAGE, """
(function_definition name: (identifier) @name) @definition
(class_definition name: (identifier) @name) @definition
""")


# --------------------------------------------------------------------------
# Data carried between stages
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ChangedSymbol:
    file: str
    name: str
    kind: str
    start_line: int
    end_line: int


@dataclass
class ParseHealth:
    ok: bool
    error_nodes: int = 0
    missing_nodes: int = 0


@dataclass
class ImportFacts:
    symbol_imported: bool = False
    module_imported: bool = False
    local_name: str | None = None


@dataclass
class Reference:
    file: str
    line: int
    local_name: str
    relation: str
    import_confirmed: bool
    confidence: float


@dataclass
class ContextItem:
    file: str
    line_range: tuple[int, int]
    relation: str
    discovery_method: str
    confidence: float
    snippet: str = ""

    @property
    def cost(self) -> int:
        return len(self.snippet)


@dataclass
class ContextBundle:
    items: list[ContextItem] = field(default_factory=list)
    dropped: list[ContextItem] = field(default_factory=list)
    status: str = "ok"


def text(node: Node) -> str:
    return node.text.decode("utf-8", "replace")


# --------------------------------------------------------------------------
# Stage 1 - parsed diff
# --------------------------------------------------------------------------

def ensure_diff_headers(patch_text: str, path: str) -> str:
    """Prepend the ---/+++ headers unidiff requires.

    GitHub's Files API returns a `patch` field that starts straight at the
    first @@ hunk header. unidiff cannot parse that. `git diff` output already
    carries the headers, so this is a no-op locally and load-bearing in
    production.
    """
    if patch_text.lstrip().startswith("@@"):
        return f"--- a/{path}\n+++ b/{path}\n{patch_text}"
    return patch_text


def parse_diff(diff_path: Path) -> dict[str, list[int]]:
    """Return the added and modified line numbers per file, in the new file."""
    patch = PatchSet(diff_path.read_text())
    changed: dict[str, list[int]] = {}
    for patched_file in patch:
        if patched_file.is_removed_file:
            continue
        lines = [
            line.target_line_no
            for hunk in patched_file
            for line in hunk
            if line.is_added and line.target_line_no is not None
        ]
        if lines:
            changed[patched_file.path] = sorted(lines)
    return changed


# --------------------------------------------------------------------------
# Stage 2 - snapshot
# --------------------------------------------------------------------------

class Snapshot:
    """The repository contents at one commit.

    The spike reads a directory. Production downloads the tarball from
    GET /repos/{owner}/{repo}/tarball/{head_sha} and extracts it. Both expose
    exactly this interface, so only the constructor changes.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self._cache: dict[str, bytes] = {}

    def python_files(self) -> list[str]:
        return sorted(
            str(p.relative_to(self.root))
            for p in self.root.rglob("*.py")
        )

    def read(self, rel_path: str) -> bytes:
        if rel_path not in self._cache:
            self._cache[rel_path] = (self.root / rel_path).read_bytes()
        return self._cache[rel_path]

    def lines(self, rel_path: str) -> list[str]:
        return self.read(rel_path).decode("utf-8", "replace").splitlines()


# --------------------------------------------------------------------------
# Stage 3 - changed lines to changed symbols
# --------------------------------------------------------------------------

def walk(node: Node):
    yield node
    for child in node.children:
        yield from walk(child)


def check_parse_health(tree) -> ParseHealth:
    """Tree-sitter never raises. A broken file yields a tree full of ERROR
    nodes, so the only way to know parsing failed is to ask."""
    if not tree.root_node.has_error:
        return ParseHealth(ok=True)
    errors = sum(1 for n in walk(tree.root_node) if n.type == "ERROR")
    missing = sum(1 for n in walk(tree.root_node) if n.is_missing)
    return ParseHealth(ok=False, error_nodes=errors, missing_nodes=missing)


def first_code_column(line: str) -> int:
    stripped = len(line) - len(line.lstrip())
    return stripped if line.strip() else 0


def resolve_changed_symbols(
    path: str, source: bytes, changed_lines: list[int]
) -> list[ChangedSymbol]:
    """Walk up from each changed line to the definition that encloses it."""
    tree = PARSER.parse(source)
    source_lines = source.decode("utf-8", "replace").splitlines()
    found: dict[tuple[str, int], ChangedSymbol] = {}

    for line_no in changed_lines:
        row = line_no - 1
        if row >= len(source_lines):
            continue
        column = first_code_column(source_lines[row])
        node = tree.root_node.named_descendant_for_point_range(
            (row, column), (row, column)
        )
        while node is not None and node.type not in DEFINITION_TYPES:
            node = node.parent

        if node is None:
            # Module-level change. Real and common, so it needs a defined
            # answer rather than an exception.
            symbol = ChangedSymbol(path, "<module>", "module", line_no, line_no)
        else:
            name_node = node.child_by_field_name("name")
            symbol = ChangedSymbol(
                file=path,
                name=text(name_node),
                kind=node.type.replace("_definition", ""),
                start_line=node.start_point[0] + 1,
                end_line=node.end_point[0] + 1,
            )
        found[(symbol.name, symbol.start_line)] = symbol

    return list(found.values())


# --------------------------------------------------------------------------
# Stage 4 - candidate finder (pass one: cheap)
# --------------------------------------------------------------------------

def find_candidates(
    snapshot: Snapshot, symbol_name: str, exclude: set[str]
) -> list[str]:
    """Literal byte scan. No parsing, deliberately. Over-returns by design."""
    needle = symbol_name.encode()
    return [
        path
        for path in snapshot.python_files()
        if path not in exclude and needle in snapshot.read(path)
    ]


# --------------------------------------------------------------------------
# Stage 5 - reference verifier (pass two: precise)
# --------------------------------------------------------------------------

def analyse_imports(tree, module_hint: str, symbol_name: str) -> ImportFacts:
    """Does this file actually import the changed symbol, and under what name?

    This is what turns a name collision into a probable real dependency.
    """
    facts = ImportFacts()
    for _, captures in QueryCursor(Q_IMPORTS).matches(tree.root_node):
        if "module_only" in captures:
            if text(captures["module_only"][0]) == module_hint:
                facts.module_imported = True
            continue

        imported = text(captures["imported"][0])
        if imported != symbol_name:
            continue
        module = text(captures["module"][0])
        if module_hint not in module:
            continue

        facts.symbol_imported = True
        facts.local_name = (
            text(captures["alias"][0]) if "alias" in captures else symbol_name
        )
    return facts


def verify_references(
    snapshot: Snapshot, path: str, symbol_name: str, module_hint: str
) -> list[Reference]:
    """Confirm each candidate really references the symbol, in code."""
    tree = PARSER.parse(snapshot.read(path))
    health = check_parse_health(tree)
    if not health.ok:
        return [
            Reference(path, 0, symbol_name, "lexical_match", False, 0.1)
        ]

    imports = analyse_imports(tree, module_hint, symbol_name)
    local_name = imports.local_name or symbol_name
    references: list[Reference] = []

    for _, captures in QueryCursor(Q_CALLS).matches(tree.root_node):
        if text(captures["name"][0]) != local_name:
            continue
        references.append(Reference(
            file=path,
            line=captures["call"][0].start_point[0] + 1,
            local_name=local_name,
            relation="inbound_call",
            import_confirmed=imports.symbol_imported,
            confidence=0.9 if imports.symbol_imported else 0.5,
        ))

    for _, captures in QueryCursor(Q_ATTRIBUTE_CALLS).matches(tree.root_node):
        if text(captures["name"][0]) != symbol_name:
            continue
        # module.symbol() is a real reference. anything_else.symbol() is a
        # different symbol that happens to share a name.
        through_module = (
            imports.module_imported
            and text(captures["object"][0]) == module_hint
        )
        references.append(Reference(
            file=path,
            line=captures["call"][0].start_point[0] + 1,
            local_name=f"{text(captures['object'][0])}.{symbol_name}",
            relation="inbound_call" if through_module else "attribute_call",
            import_confirmed=through_module,
            confidence=0.9 if through_module else 0.25,
        ))

    if not references:
        # The name is in the bytes but never called: a comment, a docstring,
        # or a log string.
        references.append(
            Reference(path, 0, symbol_name, "lexical_match", False, 0.1)
        )
    return references


# --------------------------------------------------------------------------
# Outbound definitions - what the changed code itself depends on
# --------------------------------------------------------------------------

def build_symbol_index(snapshot: Snapshot) -> dict[str, list[tuple[str, int]]]:
    index: dict[str, list[tuple[str, int]]] = {}
    for path in snapshot.python_files():
        tree = PARSER.parse(snapshot.read(path))
        if not check_parse_health(tree).ok:
            continue
        for _, captures in QueryCursor(Q_DEFINITIONS).matches(tree.root_node):
            name = text(captures["name"][0])
            line = captures["definition"][0].start_point[0] + 1
            index.setdefault(name, []).append((path, line))
    return index


def find_outbound_definitions(
    snapshot: Snapshot, symbol: ChangedSymbol, index: dict[str, list[tuple[str, int]]]
) -> list[Reference]:
    """Definitions of everything the changed symbol itself calls."""
    tree = PARSER.parse(snapshot.read(symbol.file))
    node = tree.root_node.named_descendant_for_point_range(
        (symbol.start_line - 1, 0), (symbol.end_line - 1, 0)
    )
    while node is not None and node.type not in DEFINITION_TYPES:
        node = node.parent
    if node is None:
        return []

    out: list[Reference] = []
    for _, captures in QueryCursor(Q_CALLS).matches(node):
        callee = text(captures["name"][0])
        for path, line in index.get(callee, []):
            if path == symbol.file:
                continue
            out.append(Reference(path, line, callee, "outbound_definition", True, 0.7))
    return out


# --------------------------------------------------------------------------
# Stage 6 - bundle assembler
# --------------------------------------------------------------------------

def snippet_for(snapshot: Snapshot, path: str, start: int, end: int) -> str:
    lines = snapshot.lines(path)
    lo = max(0, start - 1 - SNIPPET_PADDING)
    hi = min(len(lines), end + SNIPPET_PADDING)
    return "\n".join(lines[lo:hi])


def assemble_bundle(
    snapshot: Snapshot, references: list[Reference], budget: int = CHAR_BUDGET
) -> ContextBundle:
    """Merge references per file, rank by confidence, cut to budget."""
    kept = [r for r in references if r.confidence >= CONFIDENCE_THRESHOLD]
    merged: dict[tuple[str, str], list[Reference]] = {}
    for ref in kept:
        merged.setdefault((ref.file, ref.relation), []).append(ref)

    items: list[ContextItem] = []
    for (path, relation), refs in merged.items():
        lines = [r.line for r in refs if r.line]
        start, end = (min(lines), max(lines)) if lines else (1, 1)
        best = max(refs, key=lambda r: r.confidence)
        items.append(ContextItem(
            file=path,
            line_range=(start, end),
            relation=relation,
            discovery_method=(
                "import_edge_confirmed" if best.import_confirmed else "call_site_only"
            ),
            confidence=best.confidence,
            snippet=snippet_for(snapshot, path, start, end),
        ))

    items.sort(key=lambda i: i.confidence, reverse=True)
    bundle = ContextBundle()
    spent = 0
    for item in items:
        if spent + item.cost > budget:
            bundle.dropped.append(item)
            continue
        bundle.items.append(item)
        spent += item.cost
    if bundle.dropped:
        bundle.status = "truncated"
    return bundle


# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------

HERE = Path(__file__).parent
FIXTURE = HERE / "fixture"
SAMPLE_DIFF = HERE / "sample.diff"

# What a human says the answer should be, written before the code ran.
GROUND_TRUTH = {"service.py", "reports.py"}


def rule(title: str) -> None:
    print(f"\n{'=' * 68}\n{title}\n{'=' * 68}")


def main() -> int:
    snapshot = Snapshot(FIXTURE)

    rule("stage 1  parsed diff")
    changed = parse_diff(SAMPLE_DIFF)
    for path, lines in changed.items():
        print(f"  {path}: changed lines {lines}")

    rule("stage 2  snapshot")
    print(f"  root: {snapshot.root}")
    for path in snapshot.python_files():
        print(f"  {path}  ({len(snapshot.read(path))} bytes)")

    rule("stage 3  changed lines to changed symbols")
    symbols: list[ChangedSymbol] = []
    for path, lines in changed.items():
        health = check_parse_health(PARSER.parse(snapshot.read(path)))
        print(f"  {path}: parse ok={health.ok} "
              f"errors={health.error_nodes} missing={health.missing_nodes}")
        for symbol in resolve_changed_symbols(path, snapshot.read(path), lines):
            symbols.append(symbol)
            print(f"    {symbol.kind} {symbol.name} "
                  f"(lines {symbol.start_line}-{symbol.end_line})")

    all_references: list[Reference] = []
    index = build_symbol_index(snapshot)

    for symbol in symbols:
        if symbol.kind == "module":
            continue
        module_hint = Path(symbol.file).stem

        rule(f"stage 4  candidate finder  [byte scan for '{symbol.name}']")
        candidates = find_candidates(snapshot, symbol.name, exclude=set(changed))
        for path in candidates:
            print(f"  candidate  {path}")
        print(f"  {len(candidates)} candidates from a scan with no parsing")

        rule(f"stage 5  reference verifier  [tree-sitter, '{symbol.name}']")
        for path in candidates:
            for ref in verify_references(snapshot, path, symbol.name, module_hint):
                all_references.append(ref)
                verdict = "KEEP" if ref.confidence >= CONFIDENCE_THRESHOLD else "drop"
                where = f":{ref.line}" if ref.line else ""
                print(f"  {verdict:<4}  {ref.file}{where:<4}  {ref.relation:<16} "
                      f"as {ref.local_name:<18} import={ref.import_confirmed} "
                      f"conf={ref.confidence}")

        outbound = find_outbound_definitions(snapshot, symbol, index)
        all_references.extend(outbound)
        print(f"  outbound definitions: {len(outbound)}")

    rule("stage 6  context bundle")
    bundle = assemble_bundle(snapshot, all_references)
    print(f"  status: {bundle.status}   items: {len(bundle.items)}   "
          f"dropped: {len(bundle.dropped)}")
    for item in bundle.items:
        print(f"\n  {item.file}  lines {item.line_range[0]}-{item.line_range[1]}"
              f"  [{item.relation} / {item.discovery_method} / {item.confidence}]")
        for line in item.snippet.splitlines():
            print(f"      | {line}")

    rule("verdict against ground truth")
    retrieved = {item.file for item in bundle.items if item.relation == "inbound_call"}
    print(f"  expected:  {sorted(GROUND_TRUTH)}")
    print(f"  retrieved: {sorted(retrieved)}")
    missed = GROUND_TRUTH - retrieved
    spurious = retrieved - GROUND_TRUTH
    recall = len(GROUND_TRUTH & retrieved) / len(GROUND_TRUTH)
    print(f"  missed:    {sorted(missed) or 'none'}")
    print(f"  spurious:  {sorted(spurious) or 'none'}")
    print(f"  context recall: {recall:.0%}")
    return 0 if not missed and not spurious else 1


if __name__ == "__main__":
    raise SystemExit(main())
