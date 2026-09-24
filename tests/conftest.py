from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from github_account_analysis.attribution import AttributionRules, load_rules
from github_account_analysis.storage import AnalyticsStore


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def rules() -> AttributionRules:
    return load_rules(ROOT / "config" / "attribution-rules.v1.json")


@pytest.fixture
def store(tmp_path: Path) -> AnalyticsStore:
    return AnalyticsStore(tmp_path / "data")


def push_event(
    *,
    event_id: str,
    repository: str,
    repository_id: int,
    commits: List[Dict[str, Any]],
    created_at: str = "2025-02-01T00:10:00Z",
    actor: str = "example-user",
) -> Dict[str, Any]:
    return {
        "id": event_id,
        "type": "PushEvent",
        "created_at": created_at,
        "actor": {"login": actor},
        "repo": {"id": repository_id, "name": repository},
        "payload": {"commits": commits},
    }


def commit(
    sha: str,
    message: str,
    *,
    author_name: str = "Example Human",
    author_email: str = "human@example.test",
    timestamp: str = "2025-02-01T00:05:00Z",
) -> Dict[str, Any]:
    return {
        "sha": sha,
        "message": message,
        "timestamp": timestamp,
        "author": {"name": author_name, "email": author_email},
    }


def write_events(path: Path, events: List[Dict[str, Any]]) -> Path:
    path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
    return path
