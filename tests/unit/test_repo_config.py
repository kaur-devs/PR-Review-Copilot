"""Settings read from the repository under review.

US-004 promised that low-severity findings are hidden "unless
explicitly requested", and until now there was no way to request them.
TR-018 read "not built" beside it. The file lives in the repository so
the people being commented on own the settings, and it arrives with the
snapshot that is already being downloaded.
"""

from pathlib import Path

from src.config_file import LOCATIONS, RepoConfig, load, parse
from src.filter.gate import MINIMUM_CONFIDENCE
from src.github.review import MAX_COMMENTS_PER_REVIEW


def test_a_repository_can_ask_to_see_low_severity_findings():
    """The thing the product requirements promised and never had."""
    config = parse("severities: [high, medium, low]")

    assert "low" in config.severities


def test_a_repository_can_switch_a_category_off():
    config = parse("ignore_categories: [maintainability, performance]")

    assert config.ignore_categories == {"maintainability", "performance"}


def test_a_repository_can_tighten_the_confidence_floor():
    assert parse("minimum_confidence: 0.85").minimum_confidence == 0.85


def test_the_comment_cap_can_be_lowered_but_not_raised():
    """A repository can ask for a quieter review. It cannot ask us to
    bury a pull request under more comments than we would ever post."""
    assert parse("max_comments: 5").max_comments == 5
    assert parse("max_comments: 500").max_comments == MAX_COMMENTS_PER_REVIEW


def test_nonsense_values_fall_back_rather_than_failing_the_review():
    config = parse(
        "minimum_confidence: wildly\n"
        "severities: [urgent, high]\n"
        "ignore_categories: [vibes]\n"
        "max_comments: soon\n"
    )

    assert config.minimum_confidence == MINIMUM_CONFIDENCE
    assert config.severities == {"high"}, "the one valid entry survives"
    assert config.ignore_categories == frozenset()
    assert config.max_comments == MAX_COMMENTS_PER_REVIEW


def test_a_broken_file_is_ignored_not_fatal():
    """A reviewer that refuses to review because somebody mistyped a
    threshold is worse than one using defaults."""
    assert parse("severities: [high\n  nope: : :") == RepoConfig()
    assert parse("- just\n- a\n- list") == RepoConfig()


def test_settings_we_do_not_have_are_named_rather_than_ignored():
    config = parse("block_merge: true\nautofix: true")

    assert config.unknown == ("autofix", "block_merge")


def test_a_repository_with_no_file_gets_the_defaults(tmp_path):
    assert load(tmp_path) == RepoConfig()
    assert load(None) == RepoConfig()


def test_the_file_is_found_where_a_team_would_put_it(tmp_path):
    for location in LOCATIONS:
        target = tmp_path / location
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("max_comments: 3\n")

        assert load(tmp_path).max_comments == 3, location
        target.unlink()
