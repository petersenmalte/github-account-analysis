from __future__ import annotations

from github_account_analysis.metrics import language_distributions


def _by_language(result):
    return {row["language"]: row for row in result["distributions"]}


def test_equal_repository_and_byte_weighted_language_denominators_are_distinct() -> None:
    result = language_distributions(
        {
            "java-repository": {"Java": 90},
            "python-repository": {"Python": 10},
        }
    )
    languages = _by_language(result)

    assert languages["Java"]["equal_repository_share"] == 0.5
    assert languages["Python"]["equal_repository_share"] == 0.5
    assert languages["Java"]["byte_weighted_share"] == 0.9
    assert languages["Python"]["byte_weighted_share"] == 0.1


def test_multilanguage_empty_unknown_and_missing_repositories_have_explicit_denominators() -> None:
    result = language_distributions(
        {
            "mixed": {"Java": 50, "Python": 50},
            "python": {"Python": 100},
            "empty": {},
            "unknown-zero": {"Unknown": 0},
        }
    )
    languages = _by_language(result)

    assert result["eligible_repositories"] == 2
    assert result["repositories_without_measured_code"] == 2
    assert result["total_measured_language_bytes"] == 200
    assert languages["Java"]["equal_repository_share"] == 0.25
    assert languages["Python"]["equal_repository_share"] == 0.75
    assert languages["Java"]["byte_weighted_share"] == 0.25
    assert languages["Python"]["byte_weighted_share"] == 0.75
