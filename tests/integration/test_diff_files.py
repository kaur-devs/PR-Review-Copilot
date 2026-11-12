import pytest

from src.diff.files import ChangedFile, fetch_changed_files
from src.github import auth
from src.github.client import GitHubClient
from tests.fakes import api_file, fake_github


@pytest.fixture(autouse=True)
def forget_cached_tokens():
    auth.clear_token_cache()
    yield
    auth.clear_token_cache()


async def fetch(pages):
    client, calls = fake_github(pages)
    async with GitHubClient(42, client=client) as github:
        result = await fetch_changed_files(github, "kaur-devs/sandbox", 7)
    return result, calls


async def test_the_files_come_back():
    result, _ = await fetch([[api_file("models.py"), api_file("service.py")]])

    assert len(result) == 2
    assert [f.filename for f in result] == ["models.py", "service.py"]


async def test_it_asks_the_right_address():
    _, calls = await fetch([[api_file("models.py")]])

    data_call = [c for c in calls if not c.url.path.endswith("/access_tokens")][0]
    assert data_call.url.path == "/repos/kaur-devs/sandbox/pulls/7/files"


async def test_every_page_is_collected():
    result, _ = await fetch(
        [[api_file("a.py")], [api_file("b.py")], [api_file("c.py")]]
    )

    assert [f.filename for f in result] == ["a.py", "b.py", "c.py"]


async def test_counts_are_kept():
    result, _ = await fetch([[api_file("models.py", additions=10, deletions=3)]])

    changed = result.files[0]
    assert changed.additions == 10
    assert changed.deletions == 3
    assert changed.changes == 13
    assert result.total_changes == 13


async def test_a_file_with_no_diff_is_not_reviewable():
    result, _ = await fetch(
        [[api_file("logo.png", patch=None), api_file("models.py")]]
    )

    assert [f.filename for f in result.reviewable] == ["models.py"]
    assert [f.filename for f in result.without_patch] == ["logo.png"]


async def test_a_deleted_file_is_not_reviewable():
    result, _ = await fetch(
        [[api_file("gone.py", status="removed", patch=None), api_file("models.py")]]
    )

    assert [f.filename for f in result.reviewable] == ["models.py"]
    assert result.files[0].is_deleted
    assert result.without_patch == []


async def test_a_rename_keeps_its_old_name():
    result, _ = await fetch(
        [[api_file("new.py", status="renamed", previous_filename="old.py")]]
    )

    changed = result.files[0]
    assert changed.is_renamed
    assert changed.previous_filename == "old.py"


async def test_a_small_change_set_is_not_marked_truncated():
    result, _ = await fetch([[api_file("a.py")]])
    assert result.truncated is False


async def test_hitting_githubs_limit_is_flagged():
    page = [api_file(f"file_{n}.py") for n in range(3000)]

    result, _ = await fetch([page])

    assert len(result) == 3000
    assert result.truncated is True


def test_missing_optional_fields_do_not_crash():
    changed = ChangedFile.from_api({"filename": "a.py", "status": "modified"})

    assert changed.additions == 0
    assert changed.patch is None
    assert changed.has_patch is False
