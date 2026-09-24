from __future__ import annotations

from dataclasses import replace
import gzip
from pathlib import Path

import pytest

from github_account_analysis.ingest import EventIngestError, ingest_event_file
from github_account_analysis.metrics import build_report_data

from .conftest import commit, push_event, write_events


SHA = "a" * 40


def test_sha_deduplication_preserves_separate_repository_observations_and_replay_is_idempotent(
    store, rules, tmp_path: Path
) -> None:
    events_path = write_events(
        tmp_path / "events.jsonl",
        [
            push_event(
                event_id="one",
                repository="example/source",
                repository_id=1,
                commits=[commit(SHA, "A\n\nAI-Assisted: yes\nAI-Tools: GitHub Copilot")],
            ),
            push_event(
                event_id="two",
                repository="example/fork",
                repository_id=2,
                commits=[commit(SHA, "A\n\nAI-Assisted: yes\nAI-Tools: GitHub Copilot")],
            ),
        ],
    )

    first = ingest_event_file(
        store,
        events_path,
        source_url="https://example.test/events",
        window_start_utc="2025-02-01T00:00:00Z",
        window_end_utc="2025-02-01T01:00:00Z",
        rules=rules,
    )
    second = ingest_event_file(
        store,
        events_path,
        source_url="https://example.test/events",
        window_start_utc="2025-02-01T00:00:00Z",
        window_end_utc="2025-02-01T01:00:00Z",
        rules=rules,
    )
    report = build_report_data(store)

    assert first["unique_observed_commit_facts"] == 1
    assert first["repository_commit_observations"] == 2
    assert second["changed"] is False
    assert report["attribution"]["unique_observed_commit_facts"] == 1
    assert report["attribution"]["repository_observation_count"] == 2


def test_rule_version_change_reclassifies_without_creating_another_sha(store, rules, tmp_path: Path) -> None:
    events_path = write_events(
        tmp_path / "events.jsonl",
        [
            push_event(
                event_id="one",
                repository="example/source",
                repository_id=1,
                commits=[commit(SHA, "A\n\nAI-Assisted: yes")],
            )
        ],
    )
    ingest_event_file(
        store,
        events_path,
        source_url="https://example.test/events",
        window_start_utc="2025-02-01T00:00:00Z",
        window_end_utc="2025-02-01T01:00:00Z",
        rules=rules,
    )
    changed_rules = replace(rules, version="1.0.1", ai_trailers={"Different-Key": ("yes",)})
    ingest_event_file(
        store,
        events_path,
        source_url="https://example.test/events",
        window_start_utc="2025-02-01T00:00:00Z",
        window_end_utc="2025-02-01T01:00:00Z",
        rules=changed_rules,
    )

    rows = store.query(
        """
        SELECT sha, declared_attribution, rule_version
        FROM event_commit_attributions
        ORDER BY rule_version
        """
    )
    assert rows == [
        {"sha": SHA, "declared_attribution": "ai-assisted", "rule_version": "1.0.0"},
        {"sha": SHA, "declared_attribution": "indeterminate", "rule_version": "1.0.1"},
    ]
    report = build_report_data(store)
    assert report["attribution"]["indeterminate"] == 1
    assert report["attribution"]["declared_ai_assisted"] == 0


def test_corrupt_or_missing_event_input_records_failure_without_success(store, rules, tmp_path: Path) -> None:
    corrupt = tmp_path / "corrupt.json.gz"
    corrupt.write_bytes(b"not a gzip file")

    with pytest.raises(EventIngestError):
        ingest_event_file(
            store,
            corrupt,
            source_url="https://example.test/corrupt",
            window_start_utc="2025-02-01T00:00:00Z",
            window_end_utc="2025-02-01T01:00:00Z",
            rules=rules,
        )
    with pytest.raises(EventIngestError):
        ingest_event_file(
            store,
            tmp_path / "missing.json.gz",
            source_url="https://example.test/missing",
            window_start_utc="2025-02-01T01:00:00Z",
            window_end_utc="2025-02-01T02:00:00Z",
            rules=rules,
        )

    statuses = store.query("SELECT status, count(*) AS total FROM sources GROUP BY status")
    assert statuses == [{"status": "failed", "total": 2}]
    assert store.query("SELECT * FROM event_commits") == []


def test_month_boundaries_use_commit_timestamp_not_event_timestamp(store, rules, tmp_path: Path) -> None:
    events_path = write_events(
        tmp_path / "events.jsonl",
        [
            push_event(
                event_id="cross-month",
                repository="example/source",
                repository_id=1,
                created_at="2025-02-01T00:01:00Z",
                commits=[
                    commit(
                        SHA,
                        "A\n\nHuman-Only: yes",
                        timestamp="2025-01-31T23:59:00Z",
                    )
                ],
            )
        ],
    )
    ingest_event_file(
        store,
        events_path,
        source_url="https://example.test/month",
        window_start_utc="2025-02-01T00:00:00Z",
        window_end_utc="2025-02-01T01:00:00Z",
        rules=rules,
    )

    trend = build_report_data(store)["monthly_trend_last_ten_years"]
    assert trend == [
        {
            "month_utc": "2025-01",
            "declared_ai_assisted": 0,
            "declared_human_only": 1,
            "indeterminate": 0,
            "bot_commits": 0,
            "non_bot_commit_facts": 1,
        }
    ]


def test_repository_filter_is_recorded_and_excludes_other_event_repositories(
    store, rules, tmp_path: Path
) -> None:
    events_path = write_events(
        tmp_path / "events.jsonl",
        [
            push_event(
                event_id="keep",
                repository="example/selected",
                repository_id=1,
                commits=[commit("a" * 40, "Selected\n\nHuman-Only: yes")],
            ),
            push_event(
                event_id="skip",
                repository="example/not-selected",
                repository_id=2,
                commits=[commit("b" * 40, "Skipped\n\nAI-Assisted: yes")],
            ),
        ],
    )
    ingest_event_file(
        store,
        events_path,
        source_url="https://example.test/filtered",
        window_start_utc="2025-02-01T00:00:00Z",
        window_end_utc="2025-02-01T01:00:00Z",
        rules=rules,
        repository_filter=["example/selected"],
    )

    report = build_report_data(store)
    assert report["attribution"]["unique_observed_commit_facts"] == 1
    assert report["attribution"]["declared_human_only"] == 1
    assert "scoped to the recorded repository filter" in report["population"]["observed_event_stream"]["coverage"]


def test_filter_change_creates_new_source_and_active_scope_excludes_prior_unfiltered_facts(
    store, rules, tmp_path: Path
) -> None:
    events_path = write_events(
        tmp_path / "events.jsonl",
        [
            push_event(
                event_id="selected",
                repository="example/selected",
                repository_id=1,
                commits=[commit("a" * 40, "Selected\n\nHuman-Only: yes")],
            ),
            push_event(
                event_id="other",
                repository="example/other",
                repository_id=2,
                commits=[commit("b" * 40, "Other\n\nAI-Assisted: yes")],
            ),
        ],
    )
    unfiltered = ingest_event_file(
        store,
        events_path,
        source_url="https://example.test/same-source",
        window_start_utc="2025-02-01T00:00:00Z",
        window_end_utc="2025-02-01T01:00:00Z",
        rules=rules,
    )
    filtered = ingest_event_file(
        store,
        events_path,
        source_url="https://example.test/same-source",
        window_start_utc="2025-02-01T00:00:00Z",
        window_end_utc="2025-02-01T01:00:00Z",
        rules=rules,
        repository_filter=["example/selected"],
    )

    report = build_report_data(store)
    assert unfiltered["changed"] is True
    assert filtered["changed"] is True
    assert unfiltered["source_id"] != filtered["source_id"]
    assert report["attribution"]["unique_observed_commit_facts"] == 1
    assert report["attribution"]["declared_human_only"] == 1
    assert report["attribution"]["declared_ai_assisted"] == 0


def test_record_limit_applies_before_non_push_events_can_be_unbounded(store, rules, tmp_path: Path) -> None:
    events_path = write_events(
        tmp_path / "events.jsonl",
        [
            {"id": "one", "type": "WatchEvent"},
            {"id": "two", "type": "WatchEvent"},
        ],
    )

    with pytest.raises(EventIngestError, match="maximum of 1 records"):
        ingest_event_file(
            store,
            events_path,
            source_url="https://example.test/oversized",
            window_start_utc="2025-02-01T00:00:00Z",
            window_end_utc="2025-02-01T01:00:00Z",
            rules=rules,
            max_records=1,
        )

    assert store.query("SELECT status FROM sources") == [{"status": "failed"}]


def test_retention_prunes_old_facts_but_keeps_successful_source_provenance(
    store, rules, tmp_path: Path
) -> None:
    events_path = write_events(
        tmp_path / "old.jsonl",
        [
            push_event(
                event_id="old",
                repository="example/old",
                repository_id=1,
                commits=[commit("a" * 40, "Old\n\nHuman-Only: yes", timestamp="2010-01-01T00:00:00Z")],
            )
        ],
    )
    ingest_event_file(
        store,
        events_path,
        source_url="https://example.test/old",
        window_start_utc="2010-01-01T00:00:00Z",
        window_end_utc="2010-01-01T01:00:00Z",
        rules=rules,
    )

    assert store.query("SELECT * FROM event_commits") == []
    assert store.query("SELECT * FROM repository_commit_observations") == []
    assert store.query("SELECT status FROM sources") == [{"status": "succeeded"}]


def test_future_commit_dates_do_not_enter_historical_monthly_trend(store, rules, tmp_path: Path) -> None:
    events_path = write_events(
        tmp_path / "future.jsonl",
        [
            push_event(
                event_id="future",
                repository="example/future",
                repository_id=1,
                commits=[commit("a" * 40, "Future\n\nAI-Assisted: yes", timestamp="2099-01-01T00:00:00Z")],
            )
        ],
    )
    ingest_event_file(
        store,
        events_path,
        source_url="https://example.test/future",
        window_start_utc="2025-02-01T00:00:00Z",
        window_end_utc="2025-02-01T01:00:00Z",
        rules=rules,
    )

    assert build_report_data(store)["monthly_trend_last_ten_years"] == []
    assert store.query("SELECT * FROM event_commits") == []
    assert store.query("SELECT * FROM event_commit_attributions") == []
    assert store.query("SELECT * FROM repository_commit_observations") == []


@pytest.mark.parametrize(
    ("keyword", "limits"),
    [
        ("compressed-size", {"max_compressed_bytes": 1}),
        ("decompressed-size", {"max_decompressed_bytes": 1}),
        ("line exceeds", {"max_line_bytes": 10}),
        ("commit objects", {"max_commits": 1}),
    ],
)
def test_input_limits_are_persistent_failures(
    store, rules, tmp_path: Path, keyword: str, limits
) -> None:
    events = [
        push_event(
            event_id="limited",
            repository="example/limited",
            repository_id=1,
            commits=[
                commit("a" * 40, "A\n\nHuman-Only: yes"),
                commit("b" * 40, "B\n\nHuman-Only: yes"),
            ],
        )
    ]
    events_path = write_events(tmp_path / "limited.jsonl", events)

    with pytest.raises(EventIngestError, match=keyword):
        ingest_event_file(
            store,
            events_path,
            source_url="https://example.test/limited",
            window_start_utc="2025-02-01T00:00:00Z",
            window_end_utc="2025-02-01T01:00:00Z",
            rules=rules,
            **limits,
        )

    assert store.query("SELECT status FROM sources") == [{"status": "failed"}]


def test_attribution_is_preserved_per_selection_when_switching_back_to_prior_rules(
    store, rules, tmp_path: Path
) -> None:
    source_a = write_events(
        tmp_path / "source-a.jsonl",
        [
            push_event(
                event_id="a",
                repository="example/source",
                repository_id=1,
                commits=[commit("a" * 40, "A\n\nAI-Assisted: yes")],
            )
        ],
    )
    source_b = write_events(
        tmp_path / "source-b.jsonl",
        [
            push_event(
                event_id="b",
                repository="example/source",
                repository_id=1,
                commits=[commit("a" * 40, "A\n\nAI-Assisted: yes")],
            )
        ],
    )
    source_a_again = write_events(
        tmp_path / "source-a-again.jsonl",
        [
            push_event(
                event_id="c",
                repository="example/source",
                repository_id=1,
                commits=[commit("b" * 40, "B\n\nHuman-Only: yes")],
            )
        ],
    )
    changed_rules = replace(rules, version="1.0.1", ai_trailers={"Different-Key": ("yes",)})
    for path, source_url, active_rules in [
        (source_a, "https://example.test/a", rules),
        (source_b, "https://example.test/b", changed_rules),
        (source_a_again, "https://example.test/c", rules),
    ]:
        ingest_event_file(
            store,
            path,
            source_url=source_url,
            window_start_utc="2025-02-01T00:00:00Z",
            window_end_utc="2025-02-01T01:00:00Z",
            rules=active_rules,
        )

    report = build_report_data(store)
    assert report["attribution"]["declared_ai_assisted"] == 1
    assert report["attribution"]["declared_human_only"] == 1
    assert report["attribution"]["indeterminate"] == 0
