from __future__ import annotations

from pathlib import Path

import duckdb

from github_account_analysis.ingest import ingest_event_file
from github_account_analysis.storage import AnalyticsStore

from .conftest import commit, push_event, write_events


def test_legacy_attribution_table_is_preserved_and_replaced_with_selection_schema(
    rules, tmp_path: Path
) -> None:
    data_dir = tmp_path / "data"
    state_dir = data_dir / "state"
    state_dir.mkdir(parents=True)
    database_path = state_dir / "analytics.duckdb"
    with duckdb.connect(str(database_path)) as connection:
        connection.execute(
            """
            CREATE TABLE event_commit_attributions (
                sha VARCHAR PRIMARY KEY,
                declared_attribution VARCHAR NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO event_commit_attributions VALUES ('legacy-sha', 'ai-assisted')"
        )

    store = AnalyticsStore(data_dir)
    columns = store.query("PRAGMA table_info('event_commit_attributions')")
    names = [column["name"] for column in columns]
    legacy = store.query("SELECT * FROM event_commit_attributions_legacy")
    events_path = write_events(
        tmp_path / "events.jsonl",
        [
            push_event(
                event_id="new",
                repository="example/repository",
                repository_id=1,
                commits=[commit("a" * 40, "New\n\nHuman-Only: yes")],
            )
        ],
    )

    result = ingest_event_file(
        store,
        events_path,
        source_url="https://example.test/new",
        window_start_utc="2025-02-01T00:00:00Z",
        window_end_utc="2025-02-01T01:00:00Z",
        rules=rules,
    )

    assert names[:2] == ["sha", "event_selection_id"]
    assert legacy == [{"sha": "legacy-sha", "declared_attribution": "ai-assisted"}]
    assert result["status"] == "succeeded"
