"""Streaming ingestion for GH Archive-like NDJSON event files."""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any, BinaryIO, Collection, Dict, Iterator, List, Mapping, Optional, Tuple

import duckdb

from .attribution import AttributionRules, classify_commit
from .storage import AnalyticsStore
from .utils import canonical_json, format_utc, parse_utc, sha256_file, sha256_text, utc_now_iso


class EventIngestError(RuntimeError):
    """Input failure with no successful fresh source result."""


EVENT_ANALYSIS_SCHEMA_VERSION = "2"


def _open_event_lines(path: Path, maximum_line_bytes: int) -> Iterator[bytes]:
    if path.suffix == ".gz":
        handle: BinaryIO = gzip.open(path, mode="rb")
    else:
        handle = path.open(mode="rb")
    with handle:
        while True:
            line = handle.readline(maximum_line_bytes + 1)
            if not line:
                return
            if len(line) > maximum_line_bytes:
                raise EventIngestError(
                    f"input line exceeds configured maximum of {maximum_line_bytes} bytes"
                )
            yield line


def _as_utc_or_fallback(value: Any, fallback: str) -> str:
    if isinstance(value, str):
        try:
            return format_utc(parse_utc(value))
        except ValueError:
            pass
    return fallback


def _actor_is_bot(event: Mapping[str, Any], rules: AttributionRules) -> bool:
    actor = event.get("actor")
    if not isinstance(actor, dict):
        return False
    login = actor.get("login")
    return isinstance(login, str) and login.endswith(rules.bot_login_suffix)


def ingest_event_file(
    store: AnalyticsStore,
    path: Path,
    *,
    source_url: str,
    window_start_utc: str,
    window_end_utc: str,
    rules: AttributionRules,
    max_commits: int = 50_000,
    repository_filter: Optional[Collection[str]] = None,
    max_compressed_bytes: int = 250 * 1024 * 1024,
    max_decompressed_bytes: int = 1024 * 1024 * 1024,
    max_records: int = 1_000_000,
    max_line_bytes: int = 4 * 1024 * 1024,
) -> Dict[str, Any]:
    """Ingest an event file transactionally; corrupt inputs are persistent failures."""
    observed_at = utc_now_iso()
    normalized_filter = sorted({value.lower() for value in repository_filter or []})
    config = {
        "event_analysis_schema_version": EVENT_ANALYSIS_SCHEMA_VERSION,
        "rule_version": rules.version,
        "maximum_commit_objects": max_commits,
        "maximum_compressed_bytes": max_compressed_bytes,
        "maximum_decompressed_bytes": max_decompressed_bytes,
        "maximum_records": max_records,
        "maximum_line_bytes": max_line_bytes,
        "format": "gharchive-like-ndjson",
        "repository_filter": normalized_filter,
    }
    event_selection_id = "event-selection:" + sha256_text(
        canonical_json(
            {
                "event_analysis_schema_version": EVENT_ANALYSIS_SCHEMA_VERSION,
                "rule_version": rules.version,
                "repository_filter": normalized_filter,
            }
        )
    )
    config["event_selection_id"] = event_selection_id
    try:
        start = format_utc(parse_utc(window_start_utc))
        end = format_utc(parse_utc(window_end_utc))
        if start >= end:
            raise ValueError("window_start_utc must precede window_end_utc")
        if not path.is_file():
            raise EventIngestError(f"input event file does not exist: {path}")
        if path.stat().st_size > max_compressed_bytes:
            raise EventIngestError(
                f"input exceeds configured compressed-size limit of {max_compressed_bytes} bytes"
            )
        content_sha256 = sha256_file(path)
        source_id = "events:" + sha256_text(
            canonical_json(
                {
                    "source_url": source_url,
                    "content_sha256": content_sha256,
                    "window_start_utc": start,
                    "window_end_utc": end,
                    "ingest_configuration": config,
                }
            )
        )
        if store.has_successful_source(source_id):
            return {
                "source_id": source_id,
                "status": "succeeded",
                "changed": False,
                "message": "identical source already ingested",
            }

        records_seen = 0
        commits: Dict[str, Dict[str, Any]] = {}
        observations: Dict[Tuple[str, str], Dict[str, Any]] = {}
        commit_objects_seen = 0
        decompressed_bytes = 0
        for line_number, raw_line in enumerate(_open_event_lines(path, max_line_bytes), start=1):
            decompressed_bytes += len(raw_line)
            if decompressed_bytes > max_decompressed_bytes:
                raise EventIngestError(
                    f"input exceeds configured decompressed-size limit of {max_decompressed_bytes} bytes"
                )
            if not raw_line.strip():
                continue
            records_seen += 1
            if records_seen > max_records:
                raise EventIngestError(
                    f"input exceeds configured maximum of {max_records} records"
                )
            try:
                event = json.loads(raw_line)
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise EventIngestError(f"invalid JSON at line {line_number}") from error
            if not isinstance(event, dict):
                raise EventIngestError(f"event at line {line_number} is not a JSON object")
            if event.get("type") != "PushEvent":
                continue
            payload = event.get("payload")
            repository = event.get("repo")
            if not isinstance(payload, dict) or not isinstance(repository, dict):
                continue
            raw_commits = payload.get("commits")
            if not isinstance(raw_commits, list):
                continue
            repository_key = repository.get("name")
            if not isinstance(repository_key, str) or not repository_key:
                continue
            repository_key = repository_key.lower()
            if normalized_filter and repository_key not in normalized_filter:
                continue
            event_created_at = _as_utc_or_fallback(event.get("created_at"), start)
            actor = event.get("actor")
            actor_login = actor.get("login") if isinstance(actor, dict) else None
            if not isinstance(actor_login, str):
                actor_login = None
            for raw_commit in raw_commits:
                if not isinstance(raw_commit, dict):
                    continue
                commit_objects_seen += 1
                if commit_objects_seen > max_commits:
                    raise EventIngestError(
                        f"input exceeds configured maximum of {max_commits} commit objects"
                    )
                sha = raw_commit.get("sha")
                message = raw_commit.get("message")
                if not isinstance(sha, str) or not sha or not isinstance(message, str):
                    continue
                author = raw_commit.get("author")
                author = author if isinstance(author, dict) else {}
                author_name = author.get("name") if isinstance(author.get("name"), str) else ""
                author_email = author.get("email") if isinstance(author.get("email"), str) else ""
                author_login = author.get("username") if isinstance(author.get("username"), str) else ""
                attribution = classify_commit(
                    message,
                    author_name=author_name,
                    author_email=author_email,
                    author_login=author_login,
                    rules=rules,
                )
                commits.setdefault(
                    sha,
                    {
                        "sha": sha,
                        "message": message,
                        "author_name": author_name,
                        "author_email": author_email,
                        "author_login": author_login,
                        "committed_at_utc": _as_utc_or_fallback(
                            raw_commit.get("timestamp"), event_created_at
                        ),
                        "declared_attribution": attribution.declared_attribution,
                        "is_bot": attribution.is_bot,
                        "evidence": attribution.evidence,
                        "tool_labels": list(attribution.tool_labels),
                        "rule_version": attribution.rule_version,
                    },
                )
                observation_key = (repository_key, sha)
                observations.setdefault(
                    observation_key,
                    {
                        "repository_key": repository_key,
                        "repository_id": repository.get("id")
                        if isinstance(repository.get("id"), int)
                        else None,
                        "sha": sha,
                        "event_id": event.get("id") if isinstance(event.get("id"), str) else None,
                        "event_created_at_utc": event_created_at,
                        "actor_login": actor_login,
                        "actor_is_bot": _actor_is_bot(event, rules),
                    },
                )
        source = {
            "source_id": source_id,
            "source_url": source_url,
            "input_path": str(path),
            "content_sha256": content_sha256,
            "observed_at_utc": observed_at,
            "window_start_utc": start,
            "window_end_utc": end,
            "config_json": canonical_json(config),
            "event_selection_id": event_selection_id,
        }
        changed = store.persist_events(
            source=source,
            commits=list(commits.values()),
            observations=list(observations.values()),
            records_seen=records_seen,
        )
        return {
            "source_id": source_id,
            "status": "succeeded",
            "changed": changed,
            "records_seen": records_seen,
            "unique_observed_commit_facts": len(commits),
            "repository_commit_observations": len(observations),
        }
    except (OSError, UnicodeDecodeError, ValueError, EventIngestError, duckdb.Error) as error:
        failure_id = "failed-events:" + sha256_text(
            canonical_json(
                {
                    "source_url": source_url,
                    "input_path": str(path),
                    "observed_at": observed_at,
                    "error": str(error),
                }
            )
        )
        store.record_failure(
            source_id=failure_id,
            kind="observed-events",
            source_url=source_url,
            input_path=str(path),
            observed_at_utc=observed_at,
            window_start_utc=window_start_utc,
            window_end_utc=window_end_utc,
            config_json=canonical_json(config),
            message=str(error),
        )
        raise EventIngestError(str(error)) from error
