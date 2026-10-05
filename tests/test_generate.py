import json

import httpx
import pytest

from src.classification.kinds import API, DOCS, LOGIC, RULE_BASED, Classification
from src.context.bundle import ContextBundle, ContextItem
from src.diff.parse import parse_patch
from src.review.findings import Finding
from src.review.generate import generate
from src.review.prompts import build_system_prompt, build_user_prompt
from src.review.provider import LLMProvider

PATCH = (
    "@@ -17,3 +17,3 @@\n"
    " def get_user(user_id):\n"
    '-    """Raises KeyError."""\n'
    '+    """Returns None."""\n'
    " \n"
)


def diff_for(path="app/models.py"):
    return parse_patch(PATCH, path)


def labelled(path, kind=LOGIC):
    return {path: Classification(path, kind, RULE_BASED)}


def context_with(file="app/service.py"):
    return ContextBundle(items=[ContextItem(
        file=file, start_line=4, end_line=6,
        snippet="def user_email(i):\n    user = get_user(i)\n    return user.email",
        relation="inbound_call", discovery="import_edge_confirmed",
        confidence=0.9, changed_symbol="get_user", reference_lines=(5,),
    )])


def fake_model(findings=None, *, raw=None, status=200, record=None):
    def handle(request: httpx.Request) -> httpx.Response:
        if record is not None:
            record.append(json.loads(request.content))
        if status != 200:
            return httpx.Response(status, json={"error": "no"})
        body = raw if raw is not None else {"findings": findings or []}
        return httpx.Response(200, json={
            "model": "t",
            "choices": [{"message": {"content": json.dumps(body)}}],
        })

    return httpx.AsyncClient(transport=httpx.MockTransport(handle))


def valid_finding(**overrides):
    base = {
        "file": "app/models.py",
        "line": 18,
        "severity": "high",
        "category": "correctness",
        "message": "Callers assume a user is returned",
        "rationale": "service.py reads user.email straight after calling this.",
        "confidence": 0.8,
    }
    base.update(overrides)
    return base


async def run(model_client, diffs=None, classifications=None, context=None):
    diffs = diffs or [diff_for()]
    async with LLMProvider(api_key="k", client=model_client) as model:
        return await generate(
            model,
            diffs,
            context or ContextBundle(),
            classifications or labelled("app/models.py"),
        )


async def test_a_valid_finding_comes_through():
    findings = await run(fake_model([valid_finding()]))

    assert len(findings) == 1
    assert findings[0].file == "app/models.py"
    assert findings[0].line == 18
    assert findings[0].severity == "high"


async def test_an_empty_answer_is_fine():
    assert await run(fake_model([])) == []


async def test_a_finding_on_a_line_not_in_the_diff_is_dropped():
    """GitHub refuses comments on lines the diff does not show."""
    findings = await run(fake_model([valid_finding(line=900)]))

    assert findings == []


async def test_a_finding_about_a_file_not_in_the_change_is_dropped():
    findings = await run(fake_model([valid_finding(file="somewhere/else.py")]))

    assert findings == []


async def test_an_invented_severity_is_dropped():
    assert await run(fake_model([valid_finding(severity="catastrophic")])) == []


async def test_an_invented_category_is_dropped():
    assert await run(fake_model([valid_finding(category="vibes")])) == []


async def test_a_finding_with_no_message_is_dropped():
    assert await run(fake_model([valid_finding(message="   ")])) == []


async def test_a_missing_line_number_is_dropped():
    assert await run(fake_model([valid_finding(line=None)])) == []


async def test_confidence_given_as_text_is_still_read():
    findings = await run(fake_model([valid_finding(confidence="0.7")]))

    assert findings[0].confidence == 0.7


async def test_confidence_outside_the_range_is_clamped():
    findings = await run(fake_model([valid_finding(confidence=5)]))

    assert findings[0].confidence == 1.0


async def test_nonsense_confidence_is_dropped():
    assert await run(fake_model([valid_finding(confidence="very sure")])) == []


async def test_good_findings_survive_alongside_bad_ones():
    findings = await run(fake_model([
        valid_finding(),
        valid_finding(line=9999),
        valid_finding(severity="nope"),
        valid_finding(line=19, message="Second real problem"),
    ]))

    assert len(findings) == 2


async def test_findings_are_ordered_by_severity_then_confidence():
    findings = await run(fake_model([
        valid_finding(line=19, severity="low", confidence=0.9),
        valid_finding(line=18, severity="high", confidence=0.6),
        valid_finding(line=17, severity="medium", confidence=0.9),
    ]))

    assert [f.severity for f in findings] == ["high", "medium", "low"]


async def test_documentation_changes_are_not_sent_to_the_model():
    calls = []
    client = fake_model([], record=calls)

    findings = await run(
        client,
        diffs=[diff_for("README.md")],
        classifications=labelled("README.md", DOCS),
    )

    assert findings == []
    assert calls == []


async def test_one_request_per_kind_not_per_file():
    """Three api files and two logic files is two requests, not five."""
    calls = []
    client = fake_model([], record=calls)
    diffs = [diff_for(f"app/api_{n}.py") for n in range(3)]
    diffs += [diff_for(f"app/logic_{n}.py") for n in range(2)]

    classifications = {}
    for n in range(3):
        classifications.update(labelled(f"app/api_{n}.py", API))
    for n in range(2):
        classifications.update(labelled(f"app/logic_{n}.py", LOGIC))

    async with LLMProvider(api_key="k", client=client) as model:
        await generate(model, diffs, ContextBundle(), classifications)

    assert len(calls) == 2


async def test_the_connected_code_is_put_in_the_prompt():
    calls = []
    client = fake_model([], record=calls)

    await run(client, context=context_with())

    prompt = calls[0]["messages"][1]["content"]
    assert "app/service.py" in prompt
    assert "user.email" in prompt
    assert "uses get_user" in prompt


async def test_a_finding_records_which_other_files_were_consulted():
    findings = await run(fake_model([valid_finding()]), context=context_with())

    assert findings[0].evidence_files == ("app/service.py",)


async def test_a_model_failure_returns_nothing_rather_than_breaking():
    assert await run(fake_model(status=500)) == []


async def test_a_response_that_is_not_a_findings_list_is_handled():
    assert await run(fake_model(raw={"findings": "lots"})) == []


async def test_too_many_findings_are_capped():
    from src.review import generate as generate_module

    many = [valid_finding(line=18) for _ in range(50)]
    findings = await run(fake_model(many))

    assert len(findings) <= generate_module.MAX_FINDINGS_PER_REQUEST


def test_each_kind_gets_its_own_questions():
    api_prompt = build_system_prompt(API)
    logic_prompt = build_system_prompt(LOGIC)

    assert "route" in api_prompt
    assert "None" in logic_prompt
    assert api_prompt != logic_prompt


def test_the_prompt_shows_line_numbers():
    prompt = build_user_prompt([diff_for()], ContextBundle())

    assert "18" in prompt
    assert "app/models.py" in prompt


def test_a_finding_sorts_by_severity_first():
    high = Finding("a.py", 1, "high", "correctness", "m", "r", 0.1)
    low = Finding("a.py", 1, "low", "correctness", "m", "r", 0.9)

    assert sorted([low, high], key=lambda f: f.sort_key)[0] is high
