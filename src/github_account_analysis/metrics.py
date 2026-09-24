"""Pure and state-backed metrics with explicit denominators."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from .attribution import DECLARED_AI, DECLARED_HUMAN, INDETERMINATE
from .storage import AnalyticsStore
from .utils import parse_utc, utc_now_iso


def language_distributions(
    repository_languages: Mapping[str, Mapping[str, int]]
) -> Dict[str, Any]:
    """Calculate equal-repository and byte-weighted shares over one panel."""
    eligible: Dict[str, Mapping[str, int]] = {
        repository: languages
        for repository, languages in repository_languages.items()
        if sum(max(0, int(value)) for value in languages.values()) > 0
    }
    equal = defaultdict(float)
    weighted = Counter()
    total_bytes = 0
    for languages in eligible.values():
        repository_total = sum(max(0, int(value)) for value in languages.values())
        for language, byte_count in languages.items():
            usable = max(0, int(byte_count))
            equal[language] += usable / repository_total
            weighted[language] += usable
            total_bytes += usable
    eligible_count = len(eligible)
    languages = sorted(set(equal) | set(weighted))
    return {
        "eligible_repositories": eligible_count,
        "repositories_without_measured_code": len(repository_languages) - eligible_count,
        "total_measured_language_bytes": total_bytes,
        "distributions": [
            {
                "language": language,
                "equal_repository_share": equal[language] / eligible_count if eligible_count else 0.0,
                "byte_weighted_share": weighted[language] / total_bytes if total_bytes else 0.0,
                "language_bytes": weighted[language],
            }
            for language in languages
        ],
    }


def _source_summary(store: AnalyticsStore) -> Dict[str, Any]:
    sources = store.query(
        """
        SELECT source_id, kind, status, source_url, content_sha256, observed_at_utc,
               window_start_utc, window_end_utc, config_json, message,
               records_seen, accepted_records
        FROM sources
        ORDER BY created_at_utc, source_id
        """
    )
    active_event_selection_id = store.active_event_selection_id()
    event_sources = []
    for source in sources:
        if source["kind"] != "observed-events" or source["status"] != "succeeded":
            continue
        configuration = json.loads(source["config_json"])
        if configuration.get("event_selection_id") == active_event_selection_id:
            event_sources.append(source)
    return {
        "sources": sources,
        "successful_observed_event_source_count": len(event_sources),
        "active_event_selection_id": active_event_selection_id,
        "active_event_source_ids": [source["source_id"] for source in event_sources],
        "event_observation_window": {
            "start_utc": min(
                (source["window_start_utc"] for source in event_sources if source["window_start_utc"]),
                default=None,
            ),
            "end_utc": max(
                (source["window_end_utc"] for source in event_sources if source["window_end_utc"]),
                default=None,
            ),
        },
    }


def _monthly_trend(commits: Iterable[Mapping[str, Any]], generated_at_utc: str) -> List[Dict[str, Any]]:
    generated_at = parse_utc(generated_at_utc)
    try:
        cutoff = generated_at.replace(year=generated_at.year - 10)
    except ValueError:
        # February 29 has no equivalent in most target years.
        cutoff = generated_at.replace(year=generated_at.year - 10, day=28)
    monthly: Dict[str, Counter] = defaultdict(Counter)
    for commit in commits:
        committed_at = parse_utc(commit["committed_at_utc"])
        if committed_at < cutoff or committed_at > generated_at:
            continue
        month = committed_at.strftime("%Y-%m")
        if commit["is_bot"]:
            monthly[month]["bot"] += 1
        else:
            monthly[month][commit["declared_attribution"]] += 1
            monthly[month]["non_bot_total"] += 1
    return [
        {
            "month_utc": month,
            "declared_ai_assisted": values[DECLARED_AI],
            "declared_human_only": values[DECLARED_HUMAN],
            "indeterminate": values[INDETERMINATE],
            "bot_commits": values["bot"],
            "non_bot_commit_facts": values["non_bot_total"],
        }
        for month, values in sorted(monthly.items())
    ]


def build_report_data(store: AnalyticsStore) -> Dict[str, Any]:
    """Produce a machine-readable report basis without inventing absent coverage."""
    generated_at = utc_now_iso()
    panel_source_id = store.latest_panel_source_id()
    panel_rows: List[Mapping[str, Any]] = []
    language_rows: List[Mapping[str, Any]] = []
    panel_config: Optional[Dict[str, Any]] = None
    panel_source_observed_at_utc: Optional[str] = None
    if panel_source_id:
        panel_rows = store.query(
            "SELECT * FROM panel_repositories WHERE source_id = ? ORDER BY repository_key",
            [panel_source_id],
        )
        language_rows = store.query(
            "SELECT * FROM repository_languages WHERE source_id = ? ORDER BY repository_key, language",
            [panel_source_id],
        )
        source_config = store.query(
            "SELECT config_json, observed_at_utc FROM sources WHERE source_id = ?", [panel_source_id]
        )
        panel_config = json.loads(source_config[0]["config_json"]) if source_config else None
        panel_source_observed_at_utc = (
            source_config[0]["observed_at_utc"] if source_config else None
        )

    language_by_repository: Dict[str, Dict[str, int]] = {
        row["repository_key"]: {} for row in panel_rows
    }
    for row in language_rows:
        language_by_repository.setdefault(row["repository_key"], {})[row["language"]] = row["bytes"]
    language = language_distributions(language_by_repository)

    source_summary = _source_summary(store)
    active_event_source_ids = source_summary["active_event_source_ids"]
    if active_event_source_ids:
        placeholders = ", ".join("?" for _ in active_event_source_ids)
        commits = store.query(
            f"""
            SELECT event_commit_attributions.*
            FROM event_commit_attributions
            WHERE EXISTS (
                SELECT 1
                FROM repository_commit_observations
                WHERE repository_commit_observations.sha = event_commit_attributions.sha
                  AND repository_commit_observations.source_id IN ({placeholders})
            )
              AND event_selection_id = ?
            ORDER BY sha
            """,
            [*active_event_source_ids, source_summary["active_event_selection_id"]],
        )
        observations = store.query(
            f"""
            SELECT * FROM repository_commit_observations
            WHERE source_id IN ({placeholders})
            """,
            active_event_source_ids,
        )
    else:
        commits = []
        observations = []
    non_bot = [commit for commit in commits if not commit["is_bot"]]
    attribution = Counter(commit["declared_attribution"] for commit in non_bot)
    tool_counts: Counter[str] = Counter()
    for commit in non_bot:
        if commit["declared_attribution"] != DECLARED_AI:
            continue
        for tool in json.loads(commit["tool_labels_json"]):
            tool_counts[tool] += 1

    source_failures = [
        source for source in source_summary["sources"] if source["status"] == "failed"
    ]
    event_count = source_summary["successful_observed_event_source_count"]
    active_event_source_ids = set(source_summary["active_event_source_ids"])
    event_sources = [
        source
        for source in source_summary["sources"]
        if source["source_id"] in active_event_source_ids
    ]
    event_filters = [
        json.loads(source["config_json"]).get("repository_filter", [])
        for source in event_sources
    ]
    has_event_filter = any(event_filter for event_filter in event_filters)
    return {
        "schema_version": "report-data/v1",
        "generated_at_utc": generated_at,
        "reporting_language": (
            "Declared attribution is evidence of an explicit declaration under the named rule version; "
            "it is not proof of code origin."
        ),
        "population": {
            "repository_panel": {
                "source_id": panel_source_id,
                "selection_frame": panel_config,
                "repository_count": len(panel_rows),
                "source_data_timestamp_utc": panel_source_observed_at_utc,
                "coverage": (
                    "configured-panel snapshot" if panel_source_id else "unavailable: no successful panel source"
                ),
            },
            "observed_event_stream": {
                "coverage": (
                    (
                        "observed source windows only; scoped to the recorded repository filter; "
                        "not a complete commit census"
                        if has_event_filter
                        else "observed source windows only; not a complete commit census"
                    )
                    if event_count
                    else "unavailable: no successful observed-event sources"
                ),
                "successful_source_count": event_count,
                "active_selection_id": source_summary["active_event_selection_id"],
                "utc_window": source_summary["event_observation_window"],
            },
        },
        "attribution": {
            "primary_label": "declared-attribution",
            "unique_observed_commit_facts": len(commits),
            "repository_observation_count": len(observations),
            "non_bot_primary_denominator": len(non_bot),
            "declared_ai_assisted": attribution[DECLARED_AI],
            "declared_human_only": attribution[DECLARED_HUMAN],
            "indeterminate": attribution[INDETERMINATE],
            "bot_commit_facts": len(commits) - len(non_bot),
            "tool_breakdown_multilabel": [
                {"tool": tool, "declared_ai_assisted_commit_facts": count}
                for tool, count in sorted(tool_counts.items())
            ],
            "rule_versions": sorted({commit["rule_version"] for commit in commits}),
        },
        "language_bytes": language,
        "monthly_trend_last_ten_years": _monthly_trend(commits, generated_at),
        "sources": source_summary["sources"],
        "limitations": {
            "github_search_api": (
                "GitHub Search API has a 1,000-result cap and rate limits; it cannot establish "
                "an authoritative global repository census."
            ),
            "gharchive": (
                "GH Archive is an observed public event stream. PushEvent payload commit objects "
                "are not a complete commit census and are not equivalent to pushes."
            ),
            "historical_data": (
                "Public historical datasets and API metadata are bounded or stale populations, "
                "not a complete history."
            ),
            "biases": [
                "selection frame bias",
                "source availability and API-rate-limit bias",
                "survivorship bias",
                "time-of-day sampling bias for scheduled event collection",
                "declaration and tool-reporting bias",
            ],
            "source_failures_recorded": [
                {"source_id": source["source_id"], "message": source["message"]}
                for source in source_failures
            ],
        },
        "data_gaps": [
            "Only successfully ingested event source windows are represented; unobserved hours and months are not zero activity.",
            "PushEvent payload commit objects omit commit history outside the observed public event stream.",
            "Repository language data excludes repositories with empty or unavailable GitHub language measurements from language-share denominators.",
            (
                "Recorded source failures may leave the latest valid report stale."
                if source_failures
                else "No source failure has been recorded in the current state."
            ),
        ],
    }
