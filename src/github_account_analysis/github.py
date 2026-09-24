"""Bounded collection of public GitHub repository metadata and language bytes."""

from __future__ import annotations

import json
import os
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .storage import AnalyticsStore
from .utils import canonical_json, parse_utc, sha256_text, utc_now_iso


class CollectionError(RuntimeError):
    """A collection failure that must not look like successful fresh data."""


def normalize_repository_url(value: str) -> Tuple[str, str]:
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme != "https" or parsed.netloc.lower() != "github.com":
        raise ValueError(f"expected a https://github.com/owner/repository URL: {value}")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 2:
        raise ValueError(f"expected exactly owner/repository in URL: {value}")
    owner, repository = parts
    return owner, repository.removesuffix(".git")


def load_panel_config(path: Path) -> Dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        config = json.load(handle)
    if config.get("selection_method") not in {
        "supplied-repository-urls",
        "observed-repositories",
    }:
        raise ValueError("selection_method must be supplied-repository-urls or observed-repositories")
    repositories = config.get("repositories")
    if not isinstance(repositories, list) or not repositories:
        raise ValueError("repository panel requires at least one repository URL")
    normalized = [normalize_repository_url(value) for value in repositories]
    if len(set(normalized)) != len(normalized):
        raise ValueError("repository panel URLs must be unique after normalization")
    config["repositories"] = [
        f"https://github.com/{owner}/{repository}" for owner, repository in normalized
    ]
    return config


class PublicGitHubClient:
    """Small standard-library REST client; its only scope is public metadata."""

    def __init__(self, token: Optional[str] = None, timeout_seconds: int = 30):
        self.token = token or os.environ.get("GITHUB_TOKEN")
        self.timeout_seconds = timeout_seconds

    def get_json(self, url: str) -> Tuple[Mapping[str, Any], str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "github-account-analysis/0.1.0",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                body = response.read()
        except urllib.error.HTTPError as error:
            raise CollectionError(f"GitHub API HTTP {error.code} for {url}") from error
        except urllib.error.URLError as error:
            raise CollectionError(f"GitHub API transport failure for {url}: {error.reason}") from error
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError as error:
            raise CollectionError(f"GitHub API returned invalid JSON for {url}") from error
        if not isinstance(parsed, dict):
            raise CollectionError(f"GitHub API returned unexpected JSON shape for {url}")
        return parsed, sha256_text(body.decode("utf-8"))


def collect_panel(
    store: AnalyticsStore,
    config_path: Path,
    *,
    client: Optional[PublicGitHubClient] = None,
) -> Dict[str, Any]:
    """Collect one all-or-nothing, configured repository panel snapshot."""
    config = load_panel_config(config_path)
    client = client or PublicGitHubClient()
    payload_hashes: List[str] = []
    repositories: List[Dict[str, Any]] = []
    observed_at = utc_now_iso()
    try:
        for repository_url in config["repositories"]:
            owner, name = normalize_repository_url(repository_url)
            metadata_url = f"https://api.github.com/repos/{owner}/{name}"
            metadata, metadata_hash = client.get_json(metadata_url)
            languages_url = metadata.get("languages_url")
            if not isinstance(languages_url, str):
                raise CollectionError(f"GitHub API response missing languages_url for {repository_url}")
            languages, language_hash = client.get_json(languages_url)
            if not all(isinstance(value, int) and value >= 0 for value in languages.values()):
                raise CollectionError(f"GitHub API languages response has invalid byte counts for {repository_url}")
            payload_hashes.extend([metadata_hash, language_hash])
            full_name = metadata.get("full_name")
            html_url = metadata.get("html_url")
            repository_id = metadata.get("id")
            if not isinstance(full_name, str) or not isinstance(html_url, str) or not isinstance(repository_id, int):
                raise CollectionError(f"GitHub API metadata is incomplete for {repository_url}")
            pushed_at = metadata.get("pushed_at")
            if pushed_at is not None:
                pushed_at = parse_utc(pushed_at).isoformat().replace("+00:00", "Z")
            repositories.append(
                {
                    "repository_key": full_name.lower(),
                    "repository_id": repository_id,
                    "full_name": full_name,
                    "html_url": html_url,
                    "is_fork": bool(metadata.get("fork", False)),
                    "is_archived": bool(metadata.get("archived", False)),
                    "pushed_at_utc": pushed_at,
                    "languages": dict(sorted(languages.items())),
                }
            )
    except (CollectionError, ValueError) as error:
        failure_id = "failed-panel:" + sha256_text(
            canonical_json({"config": config, "observed_at": observed_at, "error": str(error)})
        )
        store.record_failure(
            source_id=failure_id,
            kind="repository-panel",
            source_url="https://api.github.com/",
            input_path=str(config_path),
            observed_at_utc=observed_at,
            window_start_utc=None,
            window_end_utc=None,
            config_json=canonical_json(config),
            message=str(error),
        )
        raise

    content_sha256 = sha256_text(canonical_json({"config": config, "payloads": payload_hashes}))
    source_id = f"panel:{content_sha256}"
    changed = store.persist_panel(
        source={
            "source_id": source_id,
            "source_url": "https://api.github.com/",
            "content_sha256": content_sha256,
            "observed_at_utc": observed_at,
            "config_json": canonical_json(config),
        },
        repositories=repositories,
    )
    return {
        "source_id": source_id,
        "status": "succeeded",
        "changed": changed,
        "repository_count": len(repositories),
        "observed_at_utc": observed_at,
    }
