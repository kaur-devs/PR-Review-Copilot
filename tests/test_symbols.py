import pytest

from src.context.snapshot import Snapshot
from src.context.symbols import (
    MODULE_NAME,
    changed_symbols,
    check_parse_health,
    parse_source,
    symbol_at_line,
    symbols_in_file,
)
from src.diff.parse import parse_patch

SOURCE = b'''"""Users."""

import os


class User:
    def __init__(self, name):
        self.name = name

    def get_email(self):
        return self.email


@staticmethod
def decorated():
    return 1


def get_user(user_id):
    def inner():
        return 1

    return inner()


TOTAL_USERS = 5
'''


@pytest.fixture
def tree_and_lines():
    tree = parse_source(SOURCE)
    return tree, SOURCE.decode().splitlines()


@pytest.fixture
def snapshot(tmp_path):
    (tmp_path / "models.py").write_bytes(SOURCE)
    return Snapshot(tmp_path, "o/r", "abc123")


def at(tree_and_lines, line):
    tree, lines = tree_and_lines
    return symbol_at_line(tree, lines, "models.py", line)


def test_a_line_inside_a_method_finds_the_method(tree_and_lines):
    symbol = at(tree_and_lines, 8)

    assert symbol.kind == "function"
    assert symbol.name == "__init__"
    assert symbol.qualified_name == "User.__init__"


def test_the_qualified_name_includes_the_class(tree_and_lines):
    assert at(tree_and_lines, 11).qualified_name == "User.get_email"


def test_a_line_inside_a_plain_function_finds_the_function(tree_and_lines):
    symbol = at(tree_and_lines, 23)

    assert symbol.name == "get_user"
    assert symbol.qualified_name == "get_user"


def test_a_nested_function_finds_the_innermost_one(tree_and_lines):
    symbol = at(tree_and_lines, 21)

    assert symbol.name == "inner"
    assert symbol.qualified_name == "get_user.inner"


def test_a_decorated_function_is_found(tree_and_lines):
    assert at(tree_and_lines, 16).name == "decorated"


def test_a_line_on_the_class_body_finds_the_class(tree_and_lines):
    symbol = at(tree_and_lines, 6)

    assert symbol.kind == "class"
    assert symbol.name == "User"


def test_a_module_level_line_has_a_defined_answer(tree_and_lines):
    symbol = at(tree_and_lines, 26)

    assert symbol.kind == "module"
    assert symbol.name == MODULE_NAME


def test_an_import_line_is_module_level(tree_and_lines):
    assert at(tree_and_lines, 3).kind == "module"


def test_the_symbol_knows_where_it_starts_and_ends(tree_and_lines):
    symbol = at(tree_and_lines, 23)

    assert symbol.start_line == 19
    assert symbol.end_line == 23


def test_a_line_past_the_end_of_the_file_returns_nothing(tree_and_lines):
    assert at(tree_and_lines, 9999) is None


def test_clean_source_reports_healthy():
    assert check_parse_health(parse_source(SOURCE)).ok


def test_broken_source_is_reported_rather_than_raising():
    """Tree-sitter never raises. It returns a tree full of error nodes, so the
    only way to know parsing failed is to ask."""
    tree = parse_source(b"def get_user(user_id\n    return _USERS.get(user_id\n")

    health = check_parse_health(tree)

    assert health.ok is False
    assert health.error_nodes >= 1


def test_a_broken_file_yields_no_symbols(tmp_path):
    (tmp_path / "broken.py").write_bytes(b"def get_user(user_id\n  return 1\n")
    snapshot = Snapshot(tmp_path, "o/r", "abc")

    symbols, health = symbols_in_file(snapshot, "broken.py", [1, 2])

    assert symbols == []
    assert not health.ok


def test_several_changed_lines_in_one_function_give_one_symbol(snapshot):
    symbols, _ = symbols_in_file(snapshot, "models.py", [20, 21, 23])

    names = {s.qualified_name for s in symbols}
    assert names == {"get_user.inner", "get_user"}


def test_the_same_symbol_is_not_repeated(snapshot):
    symbols, _ = symbols_in_file(snapshot, "models.py", [7, 8])

    assert [s.qualified_name for s in symbols] == ["User.__init__"]


def test_changed_symbols_reads_from_a_diff(snapshot):
    patch = (
        "@@ -19,3 +19,3 @@\n"
        " def get_user(user_id):\n"
        "     def inner():\n"
        "-        return 0\n"
        "+        return 1\n"
    )
    diff = parse_patch(patch, "models.py")

    symbols, problems = changed_symbols(snapshot, [diff])

    assert problems == {}
    assert [s.qualified_name for s in symbols] == ["get_user.inner"]


def test_a_file_missing_from_the_snapshot_is_reported(snapshot):
    diff = parse_patch("@@ -1 +1 @@\n-a\n+b", "frontend/App.jsx")

    symbols, problems = changed_symbols(snapshot, [diff])

    assert symbols == []
    assert "not in the snapshot" in problems["frontend/App.jsx"]


def test_an_unreadable_diff_is_reported(snapshot):
    diff = parse_patch("not a diff", "models.py")

    symbols, problems = changed_symbols(snapshot, [diff])

    assert symbols == []
    assert "could not be read" in problems["models.py"]


def test_module_level_changes_collapse_to_one_symbol(snapshot):
    """A new file is all additions. One module entry, not forty."""
    symbols, _ = symbols_in_file(snapshot, "models.py", [1, 2, 3, 4, 5])

    module_symbols = [s for s in symbols if s.is_module_level]
    assert len(module_symbols) == 1


def test_a_module_symbol_spans_the_whole_file(tree_and_lines):
    symbol = at(tree_and_lines, 26)

    assert symbol.start_line == 1
    assert symbol.end_line == 26
