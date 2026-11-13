"""What the rest of the code needs to know about a programming language.

Design note: docs/30_Design_Notes.md#src-context-languages
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import tree_sitter_javascript
import tree_sitter_python
import tree_sitter_typescript
from tree_sitter import Language, Parser, Query


@dataclass(frozen=True)
class LanguageSupport:
    name: str

    # Three grammars, one language as far as imports are concerned.
    family: str

    suffixes: frozenset[str]
    language: Language

    # What counts as a named definition, and what to call it in a finding.
    definition_types: frozenset[str]
    kind_by_type: dict[str, str]

    # Without these, every import line counts as a reference to itself.
    import_node_types: frozenset[str]

    calls: Query
    attribute_calls: Query
    identifiers: Query
    imports: Query

    parser: Parser = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "parser", Parser(self.language))

    def parse(self, source: bytes):
        return self.parser.parse(source)

    def is_definition(self, node) -> bool:
        """Whether this node names something a finding can be attributed to."""
        return node.type in self.definition_types

    def kind_of(self, node) -> str:
        return self.kind_by_type.get(node.type, "function")

    def normalise_import(self, text: str) -> str:
        """The module string as it should be compared against import_targets."""
        return text

    def import_targets(self, importing_file: str, defining_file: str) -> set[str]:
        """The strings an import in one file would use to name another.

        Both files are passed because the answer depends on the language. In
        Python a module's name is its own path and the importer is
        irrelevant. In JavaScript an import is a path relative to whoever
        wrote it, so the same file is "./retriever" from one place and
        "../services/retriever" from another.
        """
        raise NotImplementedError

    def is_valid_source(self, text: str) -> bool:
        """Whether this is syntactically whole, for checking a suggestion."""
        raise NotImplementedError


@dataclass(frozen=True)
class PythonSupport(LanguageSupport):
    def normalise_import(self, text: str) -> str:
        # Leading dots say how far up the package to look; we do not track it.
        return text.lstrip(".")

    def import_targets(self, importing_file: str, defining_file: str) -> set[str]:
        """app/services/retriever.py -> retriever, services.retriever, ...

        Python is unusually easy: a module's name is its path with the
        separators turned into dots, so the importer does not matter. Every
        suffix of that path is a name some import could plausibly use,
        depending on where the package root is.
        """
        parts = Path(defining_file).with_suffix("").parts
        return {".".join(parts[index:]) for index in range(len(parts))}

    def is_valid_source(self, text: str) -> bool:
        """Whether CPython would accept this as a module.

        compile() parses and runs nothing. IndentationError and TabError are
        both SyntaxError, so one except covers all three.

        This is the strongest check available and it exists only for Python.
        Other languages fall back to the parser, which is more forgiving:
        tree-sitter accepts a line de-indented out of its own function.
        """
        try:
            compile(text, "<suggestion>", "exec")
        except SyntaxError:
            return False
        except ValueError:
            # A null byte, or source nested too deeply. Not valid either.
            return False
        return True


_PYTHON_GRAMMAR = Language(tree_sitter_python.language())

PYTHON = PythonSupport(
    name="python",
    family="python",
    suffixes=frozenset({".py"}),
    language=_PYTHON_GRAMMAR,
    definition_types=frozenset({"function_definition", "class_definition"}),
    kind_by_type={"function_definition": "function", "class_definition": "class"},
    import_node_types=frozenset(
        {"import_from_statement", "dotted_name", "aliased_import"}
    ),
    calls=Query(_PYTHON_GRAMMAR, "(call function: (identifier) @name) @call"),
    attribute_calls=Query(
        _PYTHON_GRAMMAR,
        "(call function: (attribute object: (identifier) @object"
        "                          attribute: (identifier) @name)) @call",
    ),
    identifiers=Query(_PYTHON_GRAMMAR, "(identifier) @name"),
    imports=Query(
        _PYTHON_GRAMMAR,
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
    ),
)


# Three tree-sitter grammars, one set of queries.

JS_CALLS = "(call_expression function: (identifier) @name) @call"

JS_ATTRIBUTE_CALLS = """
(call_expression
  function: (member_expression
              object: (identifier) @object
              property: (property_identifier) @name)) @call
"""

JS_IDENTIFIERS = "(identifier) @name"

# Capture names are the contract; !alias stops an aliased import matching twice.
JS_IMPORTS = """
(import_statement
  (import_clause (named_imports (import_specifier !alias name: (identifier) @imported)))
  source: (string (string_fragment) @module))

(import_statement
  (import_clause (named_imports (import_specifier
                                  name: (identifier) @imported
                                  alias: (identifier) @alias)))
  source: (string (string_fragment) @module))

(import_statement
  (import_clause (namespace_import (identifier) @module_alias))
  source: (string (string_fragment) @module_only))
"""

# const chunk = (text) => ... is a declarator, so the value has to be checked.
FUNCTION_VALUES = frozenset({"arrow_function", "function_expression", "function"})

# Longest first, so ".d.ts" is tried before ".ts".
JS_OMITTABLE = (".d.ts", ".tsx", ".jsx", ".mjs", ".cjs", ".ts", ".js")


@dataclass(frozen=True)
class JavaScriptSupport(LanguageSupport):
    def is_definition(self, node) -> bool:
        if node.type in self.definition_types:
            return True
        if node.type == "variable_declarator":
            value = node.child_by_field_name("value")
            return value is not None and value.type in FUNCTION_VALUES
        return False

    def kind_of(self, node) -> str:
        return self.kind_by_type.get(node.type, "function")

    def import_targets(self, importing_file: str, defining_file: str) -> set[str]:
        """Every way an import in one file could write the path of another.

        An import is a path relative to whoever wrote it, so this is the one
        place where the importing file matters. Resolution stops at relative
        paths: a tsconfig alias such as "@/services/retriever" is not
        resolved, and the reference falls back to a lower confidence rather
        than being claimed as confirmed.
        """
        from_directory = posixpath.dirname(importing_file)
        relative = posixpath.relpath(defining_file, from_directory or ".")

        if not relative.startswith("."):
            relative = "./" + relative

        targets = {relative}

        for extension in JS_OMITTABLE:
            if relative.endswith(extension):
                without = relative[: -len(extension)]
                targets.add(without)
                # services/index.ts is also reachable as just "./services".
                if PurePosixPath(without).name == "index":
                    targets.add(posixpath.dirname(without) or ".")
                break

        return targets

    def is_valid_source(self, text: str) -> bool:
        """Whether the grammar accepts it.

        Weaker than Python's check, and worth knowing by how much. Python
        gets CPython's own parser. These get tree-sitter, which is built to
        keep going through broken code and will accept things the real
        compiler would reject. Running tsc is not an option inside a review:
        it needs the project's own config and its installed dependencies.
        """
        return not self.parse(text.encode()).root_node.has_error


def _javascript_family(
    name: str, suffixes: set[str], grammar: Language
) -> JavaScriptSupport:
    return JavaScriptSupport(
        name=name,
        family="javascript",
        suffixes=frozenset(suffixes),
        language=grammar,
        definition_types=frozenset(
            {
                "function_declaration",
                "generator_function_declaration",
                "class_declaration",
                "method_definition",
            }
        ),
        kind_by_type={
            "function_declaration": "function",
            "generator_function_declaration": "function",
            "class_declaration": "class",
            "method_definition": "function",
            "variable_declarator": "function",
        },
        import_node_types=frozenset(
            {"import_specifier", "namespace_import", "import_clause"}
        ),
        calls=Query(grammar, JS_CALLS),
        attribute_calls=Query(grammar, JS_ATTRIBUTE_CALLS),
        identifiers=Query(grammar, JS_IDENTIFIERS),
        imports=Query(grammar, JS_IMPORTS),
    )


JAVASCRIPT = _javascript_family(
    "javascript",
    {".js", ".jsx", ".mjs", ".cjs"},
    Language(tree_sitter_javascript.language()),
)

TYPESCRIPT = _javascript_family(
    "typescript",
    {".ts", ".mts", ".cts"},
    Language(tree_sitter_typescript.language_typescript()),
)

TSX = _javascript_family(
    "tsx",
    {".tsx"},
    Language(tree_sitter_typescript.language_tsx()),
)


SUPPORTED = (PYTHON, JAVASCRIPT, TYPESCRIPT, TSX)

REVIEWABLE_SUFFIXES = frozenset(
    suffix for support in SUPPORTED for suffix in support.suffixes
)


def support_for(path: str) -> LanguageSupport | None:
    """The language for this file, or None if we cannot parse it."""
    suffix = Path(path).suffix
    for support in SUPPORTED:
        if suffix in support.suffixes:
            return support
    return None


def same_language(one: str, other: str) -> bool:
    """Whether two files could possibly refer to each other.

    A TypeScript file cannot reference a Python function, so a candidate in
    another language is a coincidence of spelling and nothing more. Compared
    by family rather than by grammar, so a .tsx component and the .ts service
    it imports count as the same language.
    """
    first, second = support_for(one), support_for(other)
    return first is not None and second is not None and first.family == second.family
