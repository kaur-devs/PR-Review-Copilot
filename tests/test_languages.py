"""Languages other than Python, and the registry that holds them."""

import pytest

from src.context.languages import (
    JAVASCRIPT,
    PYTHON,
    REVIEWABLE_SUFFIXES,
    TSX,
    TYPESCRIPT,
    same_language,
    support_for,
)
from src.context.references import find_references
from src.context.snapshot import Snapshot
from src.context.symbols import symbols_in_file

RETRIEVER = '''export function retrieveContext(question: string) {
  const chunks = search(question);
  return { chunks, mean: chunks.length };
}

export class Chunker {
  chunk(text: string) { return text.split("\\n"); }
}

export const buildGraph = (chunks) => chunks.map((c) => c.id);

const LIMIT = 8;
'''

CHAT = '''import { retrieveContext, buildGraph as bg } from "../services/retriever";
import * as retriever from "../services/retriever";

export function Chat() {
  const ctx = retrieveContext("hello");
  const graph = bg(ctx.chunks);
  const other = retriever.retrieveContext("again");
  return graph;
}
'''


@pytest.fixture
def snapshot(tmp_path):
    def write(path, text):
        full = tmp_path / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(text)

    write("src/services/retriever.ts", RETRIEVER)
    write("src/components/Chat.tsx", CHAT)
    # Same function name, different language. A coincidence of spelling.
    write("backend/unrelated.py", "def retrieveContext(q):\n    return q\n")
    return Snapshot(tmp_path, "o/r", "abc123")


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------

def test_each_extension_reaches_its_own_grammar():
    assert support_for("a.py") is PYTHON
    assert support_for("a.ts") is TYPESCRIPT
    assert support_for("a.tsx") is TSX
    assert support_for("a.js") is JAVASCRIPT
    assert support_for("a.mjs") is JAVASCRIPT


def test_a_language_we_do_not_have_is_not_claimed():
    for path in ["a.css", "a.json", "a.go", "Makefile", "a"]:
        assert support_for(path) is None


def test_the_reviewable_suffixes_come_from_the_registry():
    for support in (PYTHON, TYPESCRIPT, TSX, JAVASCRIPT):
        assert support.suffixes <= REVIEWABLE_SUFFIXES


def test_the_javascript_grammars_are_one_language_to_each_other():
    # A .tsx component importing a .ts service is ordinary.
    assert same_language("a.tsx", "b.ts")
    assert same_language("a.ts", "b.js")


def test_python_and_javascript_are_not():
    assert not same_language("a.py", "b.ts")


# ---------------------------------------------------------------------------
# Resolving an import to a file
# ---------------------------------------------------------------------------

def test_python_ignores_who_is_importing():
    assert PYTHON.import_targets("anything.py", "app/models.py") == {
        "models",
        "app.models",
    }


def test_javascript_answers_differently_depending_on_who_asks():
    same = TYPESCRIPT.import_targets("src/a.ts", "src/b.ts")
    elsewhere = TYPESCRIPT.import_targets("src/deep/a.ts", "src/b.ts")

    assert "./b" in same
    assert "../b" in elsewhere


def test_the_extension_may_be_left_off_or_written_out():
    targets = TYPESCRIPT.import_targets("src/app.ts", "src/services/retriever.ts")

    assert "./services/retriever" in targets
    assert "./services/retriever.ts" in targets


def test_a_directory_can_stand_for_its_index_file():
    targets = TYPESCRIPT.import_targets("src/app.ts", "src/services/index.ts")

    assert "./services" in targets


def test_python_relative_imports_lose_their_dots():
    assert PYTHON.normalise_import(".retriever") == "retriever"
    assert PYTHON.normalise_import("..services.retriever") == "services.retriever"


def test_javascript_keeps_its_dots_because_they_are_the_path():
    assert TYPESCRIPT.normalise_import("./retriever") == "./retriever"


# ---------------------------------------------------------------------------
# Finding symbols in TypeScript
# ---------------------------------------------------------------------------

def test_every_shape_of_definition_is_found(snapshot):
    found, health = symbols_in_file(
        snapshot, "src/services/retriever.ts", [1, 6, 7, 10]
    )

    assert health.ok
    assert {(s.kind, s.qualified_name) for s in found} == {
        ("function", "retrieveContext"),
        ("class", "Chunker"),
        ("function", "Chunker.chunk"),
        ("function", "buildGraph"),
    }


def test_an_exported_definition_is_not_mistaken_for_the_module(snapshot):
    # "export function f()" wraps the declaration, so the start of the line
    # lands above it rather than inside.
    found, _ = symbols_in_file(snapshot, "src/services/retriever.ts", [1])

    assert [s.kind for s in found] == ["function"]


def test_a_plain_constant_is_not_a_definition(snapshot):
    # const LIMIT = 8 is a variable declarator too, but not a function.
    found, _ = symbols_in_file(snapshot, "src/services/retriever.ts", [11])

    assert [s.kind for s in found] == ["module"]


# ---------------------------------------------------------------------------
# Finding references across TypeScript files
# ---------------------------------------------------------------------------

def references(snapshot):
    symbols, _ = symbols_in_file(snapshot, "src/services/retriever.ts", [1, 10])
    return find_references(snapshot, symbols, {"src/services/retriever.ts"})


def test_a_tsx_file_calling_a_ts_function_is_a_confirmed_call(snapshot):
    found = [r for r in references(snapshot) if r.local_name == "retrieveContext"]

    assert found
    assert all(r.relation == "inbound_call" for r in found)
    assert all(r.import_confirmed for r in found)


def test_a_renamed_import_is_followed_to_its_local_name(snapshot):
    # import { buildGraph as bg } ... then bg(...)
    found = [r for r in references(snapshot) if r.local_name == "bg"]

    assert len(found) == 1
    assert found[0].confidence == 0.9


def test_a_namespace_import_is_followed_through_its_alias(snapshot):
    # import * as retriever ... then retriever.retrieveContext(...)
    found = [
        r for r in references(snapshot) if r.local_name == "retriever.retrieveContext"
    ]

    assert len(found) == 1
    assert found[0].confidence == 0.9


def test_a_python_file_with_the_same_name_is_not_a_candidate(snapshot):
    assert all(r.file != "backend/unrelated.py" for r in references(snapshot))


# ---------------------------------------------------------------------------
# Checking a suggestion, which is weaker outside Python
# ---------------------------------------------------------------------------

def test_the_grammar_rejects_source_it_cannot_parse():
    assert TYPESCRIPT.is_valid_source("const x = 1;")
    assert not TYPESCRIPT.is_valid_source("const x = ;")
    assert not TYPESCRIPT.is_valid_source("function f( {")


def test_python_catches_what_a_grammar_would_not():
    broken = "def f():\n    pass\nreturn 1\n"

    assert not PYTHON.is_valid_source(broken)
