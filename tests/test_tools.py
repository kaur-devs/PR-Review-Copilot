"""The toolbox an agent is given.

These run against a real snapshot on disk with no network and no model,
because the tools are the one part of the agentic design that is fully
deterministic. Everything the agent does is only as good as these.
"""

import pytest

from src.agents.tools import (
    MAX_GREP_MATCHES,
    MAX_READ_LINES,
    SCHEMAS,
    Toolbox,
    ToolFailure,
)
from src.context.snapshot import Snapshot
from src.review.provider import ToolCall

API = """\
export function streamChat(body, onToken, onDone, onSources, onError) {
  return fetch('/api/chat', { method: 'POST', body })
}

export function apiDelete(path) {
  return fetch(path, { method: 'DELETE' })
}
"""

USE_CHAT = """\
import { streamChat } from '../lib/api'

export function useChat() {
  return streamChat(body, onToken, onSources, onDone, onError)
}
"""

MODELS = """\
def get_user(user_id):
    return {"id": user_id}


class Account:
    def get_user(self):
        return None
"""

SERVICE = """\
from app.models import get_user


def email(user_id):
    return get_user(user_id)["email"]
"""


def build(tmp_path, files: dict[str, str]) -> Toolbox:
    for path, content in files.items():
        full = tmp_path / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content)
    return Toolbox(Snapshot(tmp_path, "o/r", "abc123"))


@pytest.fixture
def box(tmp_path):
    return build(tmp_path, {
        "frontend/src/lib/api.js": API,
        "frontend/src/hooks/useChat.jsx": USE_CHAT,
        "app/models.py": MODELS,
        "app/service.py": SERVICE,
        "tests/test_models.py": "from app.models import get_user\n",
    })


# ------------------------------------------------------------- read_file

def test_a_window_comes_back_with_line_numbers(box):
    answer = box.read_file("app/models.py", 1, 2)

    assert "1  def get_user(user_id):" in answer
    assert "lines 1-2 of" in answer
    assert "class Account" not in answer


def test_reading_past_the_end_stops_at_the_end(box):
    answer = box.read_file("app/models.py", 1, 9999)

    assert "class Account" in answer
    assert "9999" not in answer


def test_a_long_read_is_capped_and_says_so(tmp_path):
    box = build(tmp_path, {"big.py": "\n".join(f"line{n}" for n in range(500))})

    answer = box.read_file("big.py")

    assert f"lines 1-{MAX_READ_LINES} of 500" in answer
    assert "read 201 onwards for the rest" in answer


def test_a_missing_file_says_what_to_do_instead(box):
    with pytest.raises(ToolFailure, match="not in this repository"):
        box.read_file("app/nope.py")


# ------------------------------------------------------------------ grep

def test_grep_reports_path_and_line(box):
    answer = box.grep("streamChat")

    assert "frontend/src/lib/api.js:1:" in answer
    assert "frontend/src/hooks/useChat.jsx:1:" in answer


def test_grep_can_be_narrowed_by_suffix(box):
    answer = box.grep("get_user", glob="*.py")

    assert "app/models.py" in answer
    assert ".js" not in answer


def test_grep_can_be_narrowed_by_directory(box):
    answer = box.grep("streamChat", glob="frontend/src/hooks/")

    assert "useChat.jsx" in answer
    assert "lib/api.js" not in answer


def test_grep_finding_nothing_says_so_plainly(box):
    assert "No line contains" in box.grep("nonexistent_thing")


def test_grep_is_capped_and_admits_it(tmp_path):
    box = build(tmp_path, {
        f"f{n}.py": "needle = 1\n" for n in range(MAX_GREP_MATCHES + 20)
    })

    answer = box.grep("needle")

    assert f"stopped at {MAX_GREP_MATCHES} matches" in answer


# -------------------------------------------------------- get_definition

def test_a_definition_comes_back_with_its_source(box):
    answer = box.get_definition("apiDelete")

    assert "function apiDelete" in answer
    assert "method: 'DELETE'" in answer


def test_both_definitions_of_a_shared_name_are_shown(box):
    """get_user is a function and a method. Picking one silently would
    send the reader to the wrong place."""
    answer = box.get_definition("get_user")

    assert "get_user" in answer
    assert "Account.get_user" in answer


def test_an_unknown_symbol_explains_itself(box):
    with pytest.raises(ToolFailure, match="No definition of 'nope'"):
        box.get_definition("nope")


# ------------------------------------------------------- find_references

def test_references_carry_the_verified_confidence(box):
    """0.9 is not an opinion: an import edge and a call were both found
    in the syntax tree."""
    answer = box.find_references("get_user")

    assert "app/service.py:5" in answer
    assert "0.9" in answer
    assert "import confirmed" in answer


def test_references_work_across_javascript_too(box):
    answer = box.find_references("streamChat")

    assert "useChat.jsx" in answer
    assert "defined at frontend/src/lib/api.js" in answer


def test_a_symbol_nobody_uses_says_so(tmp_path):
    box = build(tmp_path, {"a.py": "def lonely():\n    pass\n"})

    assert "Nothing outside a.py uses lonely" in box.find_references("lonely")


# ------------------------------------------------------------ list_tests

def test_a_test_named_after_the_file_is_found(box):
    assert "tests/test_models.py" in box.list_tests("app/models.py")


def test_an_untested_file_is_reported_as_uncovered(box):
    answer = box.list_tests("frontend/src/lib/api.js")

    assert "not covered by a test" in answer


# ------------------------------------------------------------------- run

def test_run_dispatches_by_name(box):
    call = ToolCall(id="1", name="read_file", arguments='{"path": "app/models.py"}')

    assert "def get_user" in box.run(call)


def test_run_records_what_was_called(box):
    box.run(ToolCall(id="1", name="grep", arguments='{"pattern": "x"}'))
    box.run(ToolCall(id="2", name="grep", arguments='{"pattern": "y"}'))

    assert box.calls == ["grep", "grep"]


def test_a_failure_comes_back_as_text_the_model_can_act_on(box):
    """The model can recover from a missing file. It cannot recover from
    the loop stopping."""
    call = ToolCall(id="1", name="read_file", arguments='{"path": "gone.py"}')

    answer = box.run(call)

    assert answer.startswith("read_file failed:")
    assert "not in this repository" in answer


def test_an_unknown_tool_is_named_back_with_the_real_ones(box):
    answer = box.run(ToolCall(id="1", name="rm_rf", arguments="{}"))

    assert "no tool called 'rm_rf'" in answer
    assert "find_references" in answer


def test_arguments_that_are_not_json_do_not_stop_the_loop(box):
    answer = box.run(ToolCall(id="1", name="grep", arguments="{pattern: oops"))

    assert "failed" in answer


def test_the_wrong_arguments_are_explained(box):
    answer = box.run(ToolCall(id="1", name="grep", arguments='{"nope": 1}'))

    assert "wrong arguments" in answer


# --------------------------------------------------------------- schemas

def test_every_schema_matches_a_real_tool(box):
    for schema in SCHEMAS:
        name = schema["function"]["name"]
        assert hasattr(box, name), f"{name} is advertised but not implemented"


def test_every_tool_is_advertised(box):
    advertised = {schema["function"]["name"] for schema in SCHEMAS}

    assert advertised == {
        "read_file", "grep", "get_definition", "find_references", "list_tests",
    }


def test_schemas_are_shaped_the_way_the_api_wants():
    for schema in SCHEMAS:
        assert schema["type"] == "function"
        function = schema["function"]
        assert function["description"]
        assert function["parameters"]["type"] == "object"
        for required in function["parameters"]["required"]:
            assert required in function["parameters"]["properties"]
