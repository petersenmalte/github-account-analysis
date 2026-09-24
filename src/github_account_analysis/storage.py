"""DuckDB state and reproducible Parquet/status exports."""

from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import shutil
import tempfile
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence

import duckdb

from .utils import atomic_write_json, format_utc, utc_now, utc_now_iso


class AnalyticsStore:
    """Persistent state with explicit successful and failed source records."""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.state_dir = data_dir / "state"
        self.derived_dir = data_dir / "derived"
        self.temporary_dir = self.state_dir / "temporary"
        self.database_path = self.state_dir / "analytics.duckdb"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.derived_dir.mkdir(parents=True, exist_ok=True)
        self.temporary_dir.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def connect(self) -> duckdb.DuckDBPyConnection:
        connection = duckdb.connect(str(self.database_path))
        temporary_path = str(self.temporary_dir).replace("'", "''")
        connection.execute("SET memory_limit = '1GB'")
        connection.execute("SET threads = 1")
        connection.execute("SET preserve_insertion_order = false")
        connection.execute(f"SET temp_directory = '{temporary_path}'")
        return connection

    def _initialize(self) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS sources (
                    source_id VARCHAR PRIMARY KEY,
                    kind VARCHAR NOT NULL,
                    status VARCHAR NOT NULL,
                    source_url VARCHAR NOT NULL,
                    input_path VARCHAR,
                    content_sha256 VARCHAR,
                    observed_at_utc VARCHAR NOT NULL,
                    window_start_utc VARCHAR,
                    window_end_utc VARCHAR,
                    config_json VARCHAR NOT NULL,
                    message VARCHAR,
                    records_seen BIGINT NOT NULL DEFAULT 0,
                    accepted_records BIGINT NOT NULL DEFAULT 0,
                    created_at_utc VARCHAR NOT NULL
                );
                CREATE TABLE IF NOT EXISTS metadata (
                    key VARCHAR PRIMARY KEY,
                    value VARCHAR NOT NULL
                );
                CREATE TABLE IF NOT EXISTS panel_repositories (
                    source_id VARCHAR NOT NULL,
                    repository_key VARCHAR NOT NULL,
                    repository_id BIGINT,
                    full_name VARCHAR NOT NULL,
                    html_url VARCHAR NOT NULL,
                    is_fork BOOLEAN NOT NULL,
                    is_archived BOOLEAN NOT NULL,
                    pushed_at_utc VARCHAR,
                    fetched_at_utc VARCHAR NOT NULL,
                    PRIMARY KEY (source_id, repository_key)
                );
                CREATE TABLE IF NOT EXISTS repository_languages (
                    source_id VARCHAR NOT NULL,
                    repository_key VARCHAR NOT NULL,
                    language VARCHAR NOT NULL,
                    bytes BIGINT NOT NULL,
                    PRIMARY KEY (source_id, repository_key, language)
                );
                CREATE TABLE IF NOT EXISTS event_commits (
                    sha VARCHAR PRIMARY KEY,
                    message VARCHAR NOT NULL,
                    author_name VARCHAR NOT NULL,
                    author_email VARCHAR NOT NULL,
                    author_login VARCHAR NOT NULL,
                    committed_at_utc VARCHAR NOT NULL,
                    declared_attribution VARCHAR NOT NULL,
                    is_bot BOOLEAN NOT NULL,
                    evidence VARCHAR NOT NULL,
                    tool_labels_json VARCHAR NOT NULL,
                    rule_version VARCHAR NOT NULL,
                    first_source_id VARCHAR NOT NULL
                );
                CREATE TABLE IF NOT EXISTS event_commit_attributions (
                    sha VARCHAR NOT NULL,
                    event_selection_id VARCHAR NOT NULL,
                    message VARCHAR NOT NULL,
                    author_name VARCHAR NOT NULL,
                    author_email VARCHAR NOT NULL,
                    author_login VARCHAR NOT NULL,
                    committed_at_utc VARCHAR NOT NULL,
                    declared_attribution VARCHAR NOT NULL,
                    is_bot BOOLEAN NOT NULL,
                    evidence VARCHAR NOT NULL,
                    tool_labels_json VARCHAR NOT NULL,
                    rule_version VARCHAR NOT NULL,
                    PRIMARY KEY (sha, event_selection_id)
                );
                CREATE TABLE IF NOT EXISTS repository_commit_observations (
                    source_id VARCHAR NOT NULL,
                    repository_key VARCHAR NOT NULL,
                    repository_id BIGINT,
                    sha VARCHAR NOT NULL,
                    event_id VARCHAR,
                    event_created_at_utc VARCHAR NOT NULL,
                    actor_login VARCHAR,
                    actor_is_bot BOOLEAN NOT NULL,
                    PRIMARY KEY (source_id, repository_key, sha)
                );
                """
            )
            self._migrate_attribution_table_if_needed(connection)

    @staticmethod
    def _migrate_attribution_table_if_needed(connection: duckdb.DuckDBPyConnection) -> None:
        """Retire ambiguous legacy attribution rows before using selection-specific facts."""
        columns = connection.execute("PRAGMA table_info('event_commit_attributions')").fetchall()
        names = {row[1] for row in columns}
        primary_key = [
            row[1] for row in sorted(columns, key=lambda row: row[5]) if row[5] > 0
        ]
        if "event_selection_id" in names and primary_key == ["sha", "event_selection_id"]:
            return
        legacy_table = "event_commit_attributions_legacy"
        existing = connection.execute(
            """
            SELECT count(*) FROM information_schema.tables
            WHERE table_schema = 'main' AND table_name = ?
            """,
            [legacy_table],
        ).fetchone()[0]
        if existing:
            raise RuntimeError(
                "legacy attribution migration needs manual reconciliation: "
                "event_commit_attributions_legacy already exists"
            )
        connection.execute(
            "ALTER TABLE event_commit_attributions RENAME TO event_commit_attributions_legacy"
        )
        connection.execute(
            """
            CREATE TABLE event_commit_attributions (
                sha VARCHAR NOT NULL,
                event_selection_id VARCHAR NOT NULL,
                message VARCHAR NOT NULL,
                author_name VARCHAR NOT NULL,
                author_email VARCHAR NOT NULL,
                author_login VARCHAR NOT NULL,
                committed_at_utc VARCHAR NOT NULL,
                declared_attribution VARCHAR NOT NULL,
                is_bot BOOLEAN NOT NULL,
                evidence VARCHAR NOT NULL,
                tool_labels_json VARCHAR NOT NULL,
                rule_version VARCHAR NOT NULL,
                PRIMARY KEY (sha, event_selection_id)
            )
            """
        )

    @contextmanager
    def transaction(self) -> Iterator[duckdb.DuckDBPyConnection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN TRANSACTION")
            yield connection
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def has_successful_source(self, source_id: str) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT status FROM sources WHERE source_id = ?", [source_id]
            ).fetchone()
        return row is not None and row[0] == "succeeded"

    def record_failure(
        self,
        *,
        source_id: str,
        kind: str,
        source_url: str,
        input_path: Optional[str],
        observed_at_utc: str,
        window_start_utc: Optional[str],
        window_end_utc: Optional[str],
        config_json: str,
        message: str,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO sources (
                    source_id, kind, status, source_url, input_path, observed_at_utc,
                    window_start_utc, window_end_utc, config_json, message, created_at_utc
                ) VALUES (?, ?, 'failed', ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (source_id) DO UPDATE SET
                    status = 'failed', message = excluded.message, observed_at_utc = excluded.observed_at_utc
                """,
                [
                    source_id,
                    kind,
                    source_url,
                    input_path,
                    observed_at_utc,
                    window_start_utc,
                    window_end_utc,
                    config_json,
                    message,
                    utc_now_iso(),
                ],
            )
        self.export_derived()

    def persist_panel(
        self,
        *,
        source: Mapping[str, Any],
        repositories: Sequence[Mapping[str, Any]],
    ) -> bool:
        if self.has_successful_source(source["source_id"]):
            return False
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO sources (
                    source_id, kind, status, source_url, content_sha256, observed_at_utc,
                    config_json, records_seen, accepted_records, created_at_utc
                ) VALUES (?, 'repository-panel', 'succeeded', ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    source["source_id"],
                    source["source_url"],
                    source["content_sha256"],
                    source["observed_at_utc"],
                    source["config_json"],
                    len(repositories),
                    len(repositories),
                    utc_now_iso(),
                ],
            )
            for repository in repositories:
                connection.execute(
                    """
                    INSERT INTO panel_repositories VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        source["source_id"],
                        repository["repository_key"],
                        repository.get("repository_id"),
                        repository["full_name"],
                        repository["html_url"],
                        repository["is_fork"],
                        repository["is_archived"],
                        repository.get("pushed_at_utc"),
                        source["observed_at_utc"],
                    ],
                )
                for language, byte_count in repository["languages"].items():
                    connection.execute(
                        "INSERT INTO repository_languages VALUES (?, ?, ?, ?)",
                        [
                            source["source_id"],
                            repository["repository_key"],
                            language,
                            int(byte_count),
                        ],
                    )
            connection.execute(
                """
                INSERT INTO metadata VALUES ('latest_panel_source_id', ?)
                ON CONFLICT (key) DO UPDATE SET value = excluded.value
                """,
                [source["source_id"]],
            )
        self.export_derived()
        return True

    def persist_events(
        self,
        *,
        source: Mapping[str, Any],
        commits: Sequence[Mapping[str, Any]],
        observations: Sequence[Mapping[str, Any]],
        records_seen: int,
    ) -> bool:
        if self.has_successful_source(source["source_id"]):
            return False
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO sources (
                    source_id, kind, status, source_url, input_path, content_sha256,
                    observed_at_utc, window_start_utc, window_end_utc, config_json,
                    records_seen, accepted_records, created_at_utc
                ) VALUES (?, 'observed-events', 'succeeded', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    source["source_id"],
                    source["source_url"],
                    source["input_path"],
                    source["content_sha256"],
                    source["observed_at_utc"],
                    source["window_start_utc"],
                    source["window_end_utc"],
                    source["config_json"],
                    records_seen,
                    len(observations),
                    utc_now_iso(),
                ],
            )
            connection.execute(
                """
                INSERT INTO metadata VALUES ('active_event_selection_id', ?)
                ON CONFLICT (key) DO UPDATE SET value = excluded.value
                """,
                [source["event_selection_id"]],
            )
            for commit in commits:
                connection.execute(
                    """
                    INSERT INTO event_commits VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (sha) DO NOTHING
                    """,
                    [
                        commit["sha"],
                        commit["message"],
                        commit["author_name"],
                        commit["author_email"],
                        commit["author_login"],
                        commit["committed_at_utc"],
                        commit["declared_attribution"],
                        commit["is_bot"],
                        commit["evidence"],
                        json.dumps(commit["tool_labels"]),
                        commit["rule_version"],
                        source["source_id"],
                    ],
                )
                connection.execute(
                    """
                    INSERT INTO event_commit_attributions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (sha, event_selection_id) DO UPDATE SET
                        message = excluded.message,
                        author_name = excluded.author_name,
                        author_email = excluded.author_email,
                        author_login = excluded.author_login,
                        committed_at_utc = excluded.committed_at_utc,
                        declared_attribution = excluded.declared_attribution,
                        is_bot = excluded.is_bot,
                        evidence = excluded.evidence,
                        tool_labels_json = excluded.tool_labels_json,
                        rule_version = excluded.rule_version
                    """,
                    [
                        commit["sha"],
                        source["event_selection_id"],
                        commit["message"],
                        commit["author_name"],
                        commit["author_email"],
                        commit["author_login"],
                        commit["committed_at_utc"],
                        commit["declared_attribution"],
                        commit["is_bot"],
                        commit["evidence"],
                        json.dumps(commit["tool_labels"]),
                        commit["rule_version"],
                    ],
                )
            for observation in observations:
                connection.execute(
                    """
                    INSERT INTO repository_commit_observations VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (source_id, repository_key, sha) DO NOTHING
                    """,
                    [
                        source["source_id"],
                        observation["repository_key"],
                        observation.get("repository_id"),
                        observation["sha"],
                        observation.get("event_id"),
                        observation["event_created_at_utc"],
                        observation.get("actor_login"),
                        observation["actor_is_bot"],
                    ],
                )
            self._prune_event_history(connection)
        self.export_derived()
        return True

    @staticmethod
    def _retention_start_utc() -> str:
        now = utc_now()
        try:
            start = now.replace(year=now.year - 10)
        except ValueError:
            start = now.replace(year=now.year - 10, day=28)
        return format_utc(start)

    def _prune_event_history(self, connection: duckdb.DuckDBPyConnection) -> None:
        """Keep event facts/observations aligned with the ten-year report window."""
        retention_start = self._retention_start_utc()
        retention_end = utc_now_iso()
        connection.execute(
            """
            DELETE FROM repository_commit_observations
            WHERE sha IN (
                SELECT sha FROM event_commits
                WHERE committed_at_utc < ? OR committed_at_utc > ?
            )
            """,
            [retention_start, retention_end],
        )
        connection.execute(
            "DELETE FROM event_commits WHERE committed_at_utc < ? OR committed_at_utc > ?",
            [retention_start, retention_end],
        )
        connection.execute(
            """
            DELETE FROM event_commit_attributions
            WHERE committed_at_utc < ? OR committed_at_utc > ?
            """,
            [retention_start, retention_end],
        )

    def latest_panel_source_id(self) -> Optional[str]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT value FROM metadata WHERE key = 'latest_panel_source_id'"
            ).fetchone()
        return row[0] if row else None

    def active_event_selection_id(self) -> Optional[str]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT value FROM metadata WHERE key = 'active_event_selection_id'"
            ).fetchone()
        return row[0] if row else None

    def query(self, statement: str, parameters: Optional[Sequence[Any]] = None) -> List[Dict[str, Any]]:
        with self.connect() as connection:
            cursor = connection.execute(statement, parameters or [])
            columns = [item[0] for item in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def export_derived(self) -> None:
        """Export state snapshots atomically for reproducible analytical SQL consumers."""
        exports = {
            "sources.parquet": "SELECT * FROM sources ORDER BY created_at_utc, source_id",
            "observed_commit_facts.parquet": "SELECT * FROM event_commits ORDER BY sha",
            "declared_attributions.parquet": (
                "SELECT * FROM event_commit_attributions ORDER BY event_selection_id, sha"
            ),
            "repository_commit_observations.parquet": (
                "SELECT * FROM repository_commit_observations "
                "ORDER BY source_id, repository_key, sha"
            ),
            "panel_repositories.parquet": "SELECT * FROM panel_repositories ORDER BY source_id, repository_key",
            "repository_languages.parquet": (
                "SELECT * FROM repository_languages ORDER BY source_id, repository_key, language"
            ),
        }
        temporary_dir = Path(tempfile.mkdtemp(prefix=".parquet-", dir=self.derived_dir))
        try:
            with self.connect() as connection:
                for filename, statement in exports.items():
                    target = temporary_dir / filename
                    escaped = str(target).replace("'", "''")
                    connection.execute(
                        f"COPY ({statement}) TO '{escaped}' (FORMAT PARQUET, COMPRESSION ZSTD)"
                    )
            for filename in exports:
                (temporary_dir / filename).replace(self.derived_dir / filename)
        finally:
            shutil.rmtree(temporary_dir, ignore_errors=True)
        self.write_status()

    def write_status(self) -> None:
        rows = self.query(
            """
            SELECT source_id, kind, status, source_url, input_path, content_sha256,
                   observed_at_utc, window_start_utc, window_end_utc, message,
                   records_seen, accepted_records, created_at_utc
            FROM sources
            ORDER BY created_at_utc, source_id
            """
        )
        latest_panel = self.latest_panel_source_id()
        failures = [row for row in rows if row["status"] == "failed"]
        atomic_write_json(
            self.derived_dir / "current-status.json",
            {
                "schema_version": "current-status/v1",
                "generated_at_utc": utc_now_iso(),
                "latest_panel_source_id": latest_panel,
                "source_statuses": rows,
                "has_recorded_source_failures": bool(failures),
                "staleness_note": (
                    "A failed source does not refresh or replace the last valid report."
                    if failures
                    else "No recorded source failures."
                ),
            },
        )
