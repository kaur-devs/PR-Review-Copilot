from src.diff.files import ChangedFile, ChangedFileSet
from src.diff.parse import (
    SIDE_NEW,
    SIDE_OLD,
    ensure_headers,
    parse_changed_file,
    parse_changed_files,
    parse_patch,
)

SIMPLE_PATCH = """@@ -17,8 +17,8 @@
 
 
 def get_user(user_id):
-    return _USERS[user_id]
-
+    return _USERS.get(user_id)
+
 
 
 def list_active_users():"""

TWO_HUNKS = """@@ -1,2 +1,3 @@
 import os
+import sys
 def one():
@@ -20,2 +21,3 @@ def one():
 def two():
     pass
+    return None"""


def test_a_patch_from_github_is_parsed():
    diff = parse_patch(SIMPLE_PATCH, "models.py")

    assert diff.parsed
    assert len(diff.hunks) == 1


def test_added_lines_get_their_new_file_numbers():
    diff = parse_patch(SIMPLE_PATCH, "models.py")

    assert diff.added_line_numbers == [20, 21]


def test_removed_lines_get_their_old_file_numbers():
    diff = parse_patch(SIMPLE_PATCH, "models.py")

    assert [line.old_line for line in diff.removed_lines] == [20, 21]


def test_a_removed_line_has_no_new_number():
    diff = parse_patch(SIMPLE_PATCH, "models.py")

    assert all(line.new_line is None for line in diff.removed_lines)


def test_added_lines_are_on_the_right_and_removed_on_the_left():
    diff = parse_patch(SIMPLE_PATCH, "models.py")

    assert all(line.side == SIDE_NEW for line in diff.added_lines)
    assert all(line.side == SIDE_OLD for line in diff.removed_lines)


def test_the_hunk_header_numbers_are_kept():
    hunk = parse_patch(SIMPLE_PATCH, "models.py").hunks[0]

    assert hunk.old_start == 17
    assert hunk.old_length == 8
    assert hunk.new_start == 17
    assert hunk.new_length == 8


def test_several_hunks_are_all_read():
    diff = parse_patch(TWO_HUNKS, "app.py")

    assert len(diff.hunks) == 2
    assert diff.added_line_numbers == [2, 23]


def test_a_hunk_header_with_no_length_means_one_line():
    diff = parse_patch("@@ -5 +5 @@\n-old\n+new", "a.py")

    hunk = diff.hunks[0]
    assert hunk.old_length == 1
    assert hunk.new_length == 1
    assert diff.added_line_numbers == [5]


def test_the_no_newline_marker_does_not_break_parsing():
    patch = "@@ -1 +1 @@\n-old\n\\ No newline at end of file\n+new"

    diff = parse_patch(patch, "a.py")

    assert diff.parsed
    assert diff.added_line_numbers == [1]


def test_a_brand_new_file_is_all_additions():
    patch = "@@ -0,0 +1,3 @@\n+one\n+two\n+three"

    diff = parse_patch(patch, "new.py")

    assert diff.added_line_numbers == [1, 2, 3]
    assert diff.removed_lines == []


def test_we_can_comment_on_added_and_unchanged_lines():
    diff = parse_patch(SIMPLE_PATCH, "models.py")

    assert diff.can_comment_on(20)
    assert diff.can_comment_on(17)


def test_we_cannot_comment_on_a_line_outside_the_diff():
    diff = parse_patch(SIMPLE_PATCH, "models.py")

    assert not diff.can_comment_on(1)
    assert not diff.can_comment_on(500)


def test_headers_are_added_when_github_leaves_them_out():
    result = ensure_headers("@@ -1 +1 @@\n-a\n+b", "models.py")

    assert result.startswith("--- a/models.py\n+++ b/models.py\n")


def test_headers_are_not_added_twice():
    already = "--- a/models.py\n+++ b/models.py\n@@ -1 +1 @@\n-a\n+b"

    assert ensure_headers(already, "models.py") == already


def test_a_rename_keeps_the_old_name_in_the_header():
    result = ensure_headers("@@ -1 +1 @@\n-a\n+b", "new.py", previous_filename="old.py")

    assert result.startswith("--- a/old.py\n+++ b/new.py\n")


def test_a_patch_that_makes_no_sense_is_reported_not_raised():
    diff = parse_patch("this is not a diff at all", "broken.py")

    assert not diff.parsed
    assert diff.parse_error
    assert diff.hunks == []


def test_a_file_with_no_diff_is_reported():
    changed = ChangedFile(
        filename="logo.png", status="modified", additions=0, deletions=0, changes=0
    )

    diff = parse_changed_file(changed)

    assert not diff.parsed
    assert diff.parse_error == "no diff was provided"


def test_parsing_a_whole_change_set_skips_what_cannot_be_reviewed():
    file_set = ChangedFileSet(
        files=[
            ChangedFile("models.py", "modified", 2, 2, 4, SIMPLE_PATCH),
            ChangedFile("logo.png", "modified", 0, 0, 0, None),
            ChangedFile("gone.py", "removed", 0, 5, 5, None),
        ]
    )

    diffs = parse_changed_files(file_set)

    assert [d.filename for d in diffs] == ["models.py"]
    assert diffs[0].added_line_numbers == [20, 21]
