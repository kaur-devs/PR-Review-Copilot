from src.diff.files import ChangedFile
from src.diff.skip import decide, worth_reviewing

PATCH = "@@ -1 +1 @@\n-a\n+b"


def make(filename, *, status="modified", changes=2, patch=PATCH):
    return ChangedFile(
        filename=filename,
        status=status,
        additions=changes,
        deletions=0,
        changes=changes,
        patch=patch,
    )


def test_ordinary_source_files_are_reviewed():
    assert decide(make("src/models.py")).skip is False
    assert decide(make("app/components/Button.tsx")).skip is False


def test_deleted_files_are_skipped():
    decision = decide(make("gone.py", status="removed", patch=None))

    assert decision.skip
    assert "deleted" in decision.reason


def test_files_with_no_diff_are_skipped():
    decision = decide(make("logo.png", patch=None))

    assert decision.skip
    assert "no diff" in decision.reason


def test_lockfiles_are_skipped():
    for name in ["package-lock.json", "poetry.lock", "go.sum", "Cargo.lock"]:
        assert decide(make(name)).skip


def test_a_lockfile_in_a_subfolder_is_still_skipped():
    assert decide(make("backend/poetry.lock")).skip


def test_vendored_code_is_skipped():
    for path in ["node_modules/left-pad/index.js", "vendor/lib.go", "dist/bundle.js"]:
        assert decide(make(path)).skip


def test_machine_generated_files_are_skipped():
    for path in ["app.min.js", "styles.min.css", "bundle.js.map", "api_pb2.py"]:
        assert decide(make(path)).skip


def test_an_enormous_file_change_is_skipped():
    decision = decide(make("huge.py", changes=5000))

    assert decision.skip
    assert "5000" in decision.reason


def test_a_change_just_under_the_limit_is_kept():
    assert decide(make("big.py", changes=500)).skip is False


def test_a_file_named_like_a_lockfile_but_not_one_is_reviewed():
    assert decide(make("lockfile_helper.py")).skip is False


def test_splitting_a_set_keeps_the_reasons():
    keep, skipped = worth_reviewing([
        make("src/models.py"),
        make("package-lock.json"),
        make("node_modules/x.js"),
        make("src/service.py"),
    ])

    assert [f.filename for f in keep] == ["src/models.py", "src/service.py"]
    assert set(skipped) == {"package-lock.json", "node_modules/x.js"}
    assert skipped["package-lock.json"] == "it is a lockfile"
