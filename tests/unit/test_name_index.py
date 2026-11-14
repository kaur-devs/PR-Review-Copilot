"""Extracting the names a repository contains, and the ones a diff removes.

The defect this exists for: a key renamed in a Python response and still
read as a property in TypeScript. There is no call, no import and no
symbol to resolve between those two — only a word in common.
"""

from src.context.languages import support_for
from src.diff.parse import parse_patch
from src.index.names import (
    TOO_COMMON,
    before_text,
    names_in,
    names_in_diff,
    removed_names,
    worth_indexing,
)

PY = support_for("x.py")
TS = support_for("x.ts")
TSX = support_for("x.tsx")


def kinds_of(source: bytes, support, text: str) -> set[str]:
    return {n.kind for n in names_in(source, support) if n.text == text}


# ------------------------------------------------- what gets indexed

def test_a_dictionary_key_is_indexed_not_just_the_symbols():
    """Indexing identifiers alone is what makes the renamed-key defect
    invisible, which is the behaviour this replaces."""
    source = b'def as_json(flag):\n    return {"rollout_percent": flag.enabled}\n'

    assert "key" in kinds_of(source, PY, "rollout_percent")


def test_a_typescript_interface_property_is_indexed():
    """The far end of a renamed backend key is usually a type declaration."""
    source = b"export interface Flag { rollout_percent: number; }\n"

    assert "key" in kinds_of(source, TS, "rollout_percent")


def test_a_property_access_is_indexed_in_both_languages():
    assert "property" in kinds_of(b"x = flag.rollout_percent\n", PY, "rollout_percent")
    assert "property" in kinds_of(
        b"const x = flag.rollout_percent;\n", TSX, "rollout_percent"
    )


def test_a_keyword_argument_is_not_called_a_key():
    """`rollout_percent=body.rollout_percent` elsewhere in a file made a
    removed dictionary key of the same name look like it was still there,
    which hid the rename completely."""
    source = b"f = Flag(rollout_percent=body.rollout_percent)\n"

    assert kinds_of(source, PY, "rollout_percent") == {"argument", "property"}


def test_a_word_in_every_file_is_not_worth_indexing():
    assert not worth_indexing("id")
    assert not worth_indexing("data")
    assert not worth_indexing("__init__")
    assert worth_indexing("rollout_percent")
    assert "self" in TOO_COMMON


# --------------------------------------------- what a change takes away

def test_a_renamed_symbol_is_reported_as_removed():
    before = names_in(b"def calculate_total(x):\n    return x\n", PY)
    after = names_in(b"def compute_total(x):\n    return x\n", PY)

    assert removed_names(before, after) == {"calculate_total"}


def test_a_role_lost_counts_even_when_the_word_survives():
    """Renaming a response key leaves `flag.rollout_percent` on the same
    line as a property access, so the word survives the change. What
    disappeared is the key, and comparing roles is what sees that."""
    before = names_in(b'return {"rollout_percent": f.rollout_percent}\n', PY)
    after = names_in(b'return {"rollout": f.rollout_percent}\n', PY)

    assert removed_names(before, after) == {"rollout_percent"}


def test_adding_a_name_removes_nothing():
    before = names_in(b"def a():\n    pass\n", PY)
    after = names_in(b"def a():\n    pass\n\ndef b():\n    pass\n", PY)

    assert removed_names(before, after) == set()


# ------------------------------------------- rebuilding the old version

AFTER = """def as_json(flag):
    return {
        "key": flag.key,
        "rollout": flag.rollout_percent,
        "enabled": flag.enabled,
    }
"""

PATCH = """@@ -1,6 +1,6 @@
 def as_json(flag):
     return {
         "key": flag.key,
-        "rollout_percent": flag.rollout_percent,
+        "rollout": flag.rollout_percent,
         "enabled": flag.enabled,
     }
"""


def test_the_old_version_of_a_file_is_rebuilt_from_the_diff():
    """Parsing the removed lines alone does not work: out of its
    dictionary, `"rollout_percent": flag.rollout_percent,` is not a
    dictionary pair, so the query that finds keys never matches."""
    diff = parse_patch(PATCH, "flags.py")

    rebuilt = before_text(diff, AFTER)

    assert '"rollout_percent": flag.rollout_percent,' in rebuilt
    assert '"rollout": flag.rollout_percent,' not in rebuilt
    assert '"enabled": flag.enabled,' in rebuilt, "context lines survive"


def test_the_real_rename_is_found_from_a_real_diff():
    diff = parse_patch(PATCH, "flags.py")

    assert names_in_diff(diff, AFTER, PY) == {"rollout_percent"}


def test_a_diff_that_only_adds_removes_nothing():
    patch = "@@ -1,1 +1,2 @@\n def a():\n+    pass\n"
    diff = parse_patch(patch, "x.py")

    assert names_in_diff(diff, "def a():\n    pass\n", PY) == set()


def test_an_unparseable_file_costs_a_candidate_not_a_review():
    assert names_in(b"def (((( broken\n", PY) is not None
