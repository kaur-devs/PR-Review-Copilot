import pytest

from src.context.bundle import (
    DISCOVERY_CALL_ONLY,
    DISCOVERY_IMPORT_CONFIRMED,
    STATUS_DEGRADED,
    STATUS_OK,
    assemble,
)
from src.context.references import INBOUND_CALL, LEXICAL_MATCH, Reference
from src.context.snapshot import Snapshot
from src.context.symbols import Symbol

GET_USER = Symbol("get_user", "get_user", "function", "app/models.py", 10, 12)

SERVICE = """from app.models import get_user


def user_email(user_id):
    user = get_user(user_id)
    return user.email


def other():
    return 1
"""


@pytest.fixture
def snapshot(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "service.py").write_text(SERVICE)
    (tmp_path / "app" / "wide.py").write_text(
        "from app.models import get_user\n\nhandler = get_user\n"
    )
    return Snapshot(tmp_path, "o/r", "abc123")


def ref(file, line, *, confidence=0.9, import_confirmed=True, relation=INBOUND_CALL):
    return Reference(file, line, GET_USER, "get_user", relation, import_confirmed, confidence)


def test_a_strong_reference_becomes_an_item(snapshot):
    bundle = assemble(snapshot, [ref("app/service.py", 5)])

    assert len(bundle) == 1
    assert bundle.items[0].file == "app/service.py"
    assert bundle.items[0].changed_symbol == "get_user"


def test_the_snippet_is_the_whole_enclosing_function(snapshot):
    bundle = assemble(snapshot, [ref("app/service.py", 5)])

    item = bundle.items[0]
    assert item.start_line == 4
    assert item.end_line == 6
    assert "def user_email" in item.snippet
    assert "return user.email" in item.snippet
    assert "def other" not in item.snippet


def test_weak_references_are_left_out(snapshot):
    bundle = assemble(snapshot, [
        ref("app/service.py", 5),
        ref("app/service.py", 0, confidence=0.1, relation=LEXICAL_MATCH),
    ])

    assert len(bundle) == 1


def test_several_uses_in_one_file_become_one_item(snapshot):
    bundle = assemble(snapshot, [ref("app/service.py", 5), ref("app/service.py", 6)])

    assert len(bundle) == 1
    assert bundle.items[0].reference_lines == (5, 6)


def test_provenance_records_whether_the_import_was_confirmed(snapshot):
    confirmed = assemble(snapshot, [ref("app/service.py", 5)])
    unconfirmed = assemble(
        snapshot, [ref("app/service.py", 5, confidence=0.5, import_confirmed=False)]
    )

    assert confirmed.items[0].discovery == DISCOVERY_IMPORT_CONFIRMED
    assert unconfirmed.items[0].discovery == DISCOVERY_CALL_ONLY


def test_items_are_ordered_by_confidence(snapshot):
    bundle = assemble(snapshot, [
        ref("app/wide.py", 3, confidence=0.5, import_confirmed=False),
        ref("app/service.py", 5, confidence=0.9),
    ])

    assert [item.file for item in bundle.items] == ["app/service.py", "app/wide.py"]


def test_the_budget_drops_the_weakest_first(snapshot):
    """With room for one, the stronger reference wins."""
    both = assemble(snapshot, [
        ref("app/service.py", 5, confidence=0.9),
        ref("app/wide.py", 3, confidence=0.5, import_confirmed=False),
    ])
    strongest_cost = both.items[0].cost

    bundle = assemble(
        snapshot,
        [
            ref("app/service.py", 5, confidence=0.9),
            ref("app/wide.py", 3, confidence=0.5, import_confirmed=False),
        ],
        budget=strongest_cost,
    )

    assert [item.file for item in bundle.items] == ["app/service.py"]
    assert [item.file for item in bundle.dropped] == ["app/wide.py"]


def test_dropping_anything_marks_the_bundle_degraded(snapshot):
    bundle = assemble(snapshot, [ref("app/service.py", 5)], budget=5)

    assert bundle.status == STATUS_DEGRADED


def test_a_clean_run_is_marked_ok(snapshot):
    assert assemble(snapshot, [ref("app/service.py", 5)]).status == STATUS_OK


def test_unparsed_files_are_carried_through_as_problems(snapshot):
    bundle = assemble(snapshot, [ref("app/service.py", 5)], {"x.py": "did not parse"})

    assert bundle.status == STATUS_DEGRADED
    assert bundle.problems == {"x.py": "did not parse"}


def test_an_empty_bundle_is_valid(snapshot):
    bundle = assemble(snapshot, [])

    assert len(bundle) == 0
    assert bundle.characters == 0
    assert bundle.status == STATUS_OK


def test_an_enormous_function_is_truncated(tmp_path, monkeypatch):
    from src.context import bundle as bundle_module

    monkeypatch.setattr(bundle_module, "MAX_SNIPPET_LINES", 5)
    body = "\n".join(f"    line_{n} = {n}" for n in range(50))
    (tmp_path / "big.py").write_text(f"def huge():\n{body}\n")
    snapshot = Snapshot(tmp_path, "o/r", "abc")

    result = assemble(snapshot, [ref("big.py", 3)])

    assert "truncated" in result.items[0].snippet
    assert len(result.items[0].snippet.splitlines()) == 6
