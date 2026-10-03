import pytest

from src.context.references import (
    ATTRIBUTE_CALL,
    INBOUND_CALL,
    INBOUND_REFERENCE,
    LEXICAL_MATCH,
    find_candidates,
    find_references,
    module_names_for,
    verify_references,
)
from src.context.snapshot import Snapshot
from src.context.symbols import Symbol

GET_USER = Symbol(
    name="get_user", qualified_name="get_user", kind="function",
    file="app/models.py", start_line=10, end_line=12,
)


def build(tmp_path, files: dict[str, str]) -> Snapshot:
    for path, content in files.items():
        full = tmp_path / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content)
    return Snapshot(tmp_path, "o/r", "abc123")


def verify(tmp_path, files, path, symbol=GET_USER):
    return verify_references(build(tmp_path, files), path, symbol)


def test_a_plain_import_and_call_is_strong(tmp_path):
    refs = verify(tmp_path, {
        "app/service.py": "from app.models import get_user\n\n"
                          "def email(i):\n    return get_user(i).email\n",
    }, "app/service.py")

    assert len(refs) == 1
    assert refs[0].relation == INBOUND_CALL
    assert refs[0].import_confirmed
    assert refs[0].confidence == 0.9
    assert refs[0].line == 4


def test_a_renamed_import_is_followed(tmp_path):
    """The call says fetch_user. Only reading the import connects them."""
    refs = verify(tmp_path, {
        "app/reports.py": "from app.models import get_user as fetch_user\n\n"
                          "def line(i):\n    return fetch_user(i)\n",
    }, "app/reports.py")

    assert refs[0].relation == INBOUND_CALL
    assert refs[0].local_name == "fetch_user"
    assert refs[0].confidence == 0.9


def test_a_name_only_in_comments_and_strings_is_rejected(tmp_path):
    refs = verify(tmp_path, {
        "app/billing.py": '"""Once called get_user directly."""\n\n'
                          'NOTE = "get_user was consulted"\n\n'
                          "def charge(a):\n    # get_user is not used here\n    return a\n",
    }, "app/billing.py")

    assert refs[0].relation == LEXICAL_MATCH
    assert refs[0].confidence == 0.1
    assert not refs[0].is_strong


def test_a_method_on_another_object_is_rejected(tmp_path):
    """client.get_user is a different function that shares a name."""
    refs = verify(tmp_path, {
        "app/notify.py": "from external import Client\n\nclient = Client()\n\n"
                         "def notify(i):\n    return client.get_user(i)\n",
    }, "app/notify.py")

    assert refs[0].relation == ATTRIBUTE_CALL
    assert refs[0].confidence == 0.25
    assert not refs[0].is_strong


def test_a_call_through_the_module_is_strong(tmp_path):
    """models.get_user() is genuinely our function."""
    refs = verify(tmp_path, {
        "app/worker.py": "import models\n\ndef run(i):\n    return models.get_user(i)\n",
    }, "app/worker.py")

    assert refs[0].relation == INBOUND_CALL
    assert refs[0].import_confirmed
    assert refs[0].confidence == 0.9


def test_a_call_with_no_import_is_weaker(tmp_path):
    refs = verify(tmp_path, {
        "app/odd.py": "def run(i):\n    return get_user(i)\n",
    }, "app/odd.py")

    assert refs[0].relation == INBOUND_CALL
    assert not refs[0].import_confirmed
    assert refs[0].confidence == 0.5


def test_passing_the_function_without_calling_it_counts(tmp_path):
    """handler = get_user is a real dependency, just not a call."""
    refs = verify(tmp_path, {
        "app/wire.py": "from app.models import get_user\n\nhandler = get_user\n",
    }, "app/wire.py")

    assert refs[0].relation == INBOUND_REFERENCE
    assert refs[0].confidence == 0.7


def test_the_import_line_itself_is_not_counted_as_a_use(tmp_path):
    refs = verify(tmp_path, {
        "app/only_imports.py": "from app.models import get_user\n",
    }, "app/only_imports.py")

    assert len(refs) == 1
    assert refs[0].relation == LEXICAL_MATCH


def test_every_call_site_is_reported(tmp_path):
    refs = verify(tmp_path, {
        "app/service.py": "from app.models import get_user\n\n"
                          "def a(i):\n    return get_user(i)\n\n"
                          "def b(i):\n    return get_user(i)\n",
    }, "app/service.py")

    calls = [r for r in refs if r.relation == INBOUND_CALL]
    assert [r.line for r in calls] == [4, 7]


def test_a_file_that_does_not_parse_falls_back_to_lexical(tmp_path):
    refs = verify(tmp_path, {
        "app/broken.py": "from app.models import get_user\ndef x(\n    get_user(\n",
    }, "app/broken.py")

    assert refs[0].relation == LEXICAL_MATCH


def test_module_names_cover_every_import_style():
    assert module_names_for("app/models.py") == {"models", "app.models"}
    assert module_names_for("models.py") == {"models"}
    assert module_names_for("a/b/c.py") == {"c", "b.c", "a.b.c"}


def test_candidates_are_found_by_plain_text(tmp_path):
    snapshot = build(tmp_path, {
        "app/models.py": "def get_user(i): pass\n",
        "app/service.py": "get_user(1)\n",
        "app/other.py": "something_else()\n",
    })

    found = find_candidates(snapshot, "get_user", exclude={"app/models.py"})

    assert found == ["app/service.py"]


def test_candidates_exclude_the_changed_files(tmp_path):
    snapshot = build(tmp_path, {
        "app/models.py": "def get_user(i): pass\n",
        "app/service.py": "get_user(1)\n",
    })

    found = find_candidates(snapshot, "get_user", exclude={"app/models.py", "app/service.py"})

    assert found == []


def test_the_candidate_list_is_capped(tmp_path, monkeypatch):
    from src.context import references

    monkeypatch.setattr(references, "MAX_CANDIDATES_PER_SYMBOL", 3)
    files = {f"app/f{n}.py": "get_user(1)\n" for n in range(10)}
    snapshot = build(tmp_path, files)

    assert len(find_candidates(snapshot, "get_user", exclude=set())) == 3


def test_the_whole_search_keeps_real_uses_and_drops_the_rest(tmp_path):
    snapshot = build(tmp_path, {
        "app/models.py": "def get_user(i):\n    return None\n",
        "app/service.py": "from app.models import get_user\n\ndef e(i):\n    return get_user(i)\n",
        "app/reports.py": "from app.models import get_user as fetch\n\ndef r(i):\n    return fetch(i)\n",
        "app/billing.py": "# get_user used to be called here\nNOTE = 'get_user'\n",
        "app/notify.py": "client = None\n\ndef n(i):\n    return client.get_user(i)\n",
    })

    refs = find_references(snapshot, [GET_USER], changed_files={"app/models.py"})

    strong = {r.file for r in refs if r.is_strong}
    assert strong == {"app/service.py", "app/reports.py"}


def test_module_level_symbols_are_not_searched_for(tmp_path):
    module_symbol = Symbol("<module>", "<module>", "module", "app/models.py", 1, 20)
    snapshot = build(tmp_path, {"app/models.py": "x = 1\n", "app/other.py": "y = 2\n"})

    assert find_references(snapshot, [module_symbol], changed_files=set()) == []
