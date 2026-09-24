"""Bounded anonymous GitHub REST API collector; it never clones or executes targets."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

API_BASE = "https://api.github.com"
USER_AGENT = "github-account-analysis/0.1"


class GitHubAPIError(RuntimeError):
    """An API error whose details are safe to surface as incomplete evidence."""


def normalize_login(value: str) -> str:
    """Accept a GitHub username or github.com profile URL, never arbitrary URLs."""
    value = (value or "").strip()
    parsed = urlparse(value if "://" in value else f"https://github.com/{value}")
    if parsed.hostname not in {"github.com", "www.github.com"}:
        raise ValueError("Enter a GitHub username or an https://github.com/<username> profile URL.")
    login = parsed.path.strip("/").split("/")[0]
    if not login or any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-" for character in login):
        raise ValueError("The GitHub username is invalid.")
    return login


class GitHubClient:
    """Small, cached, rate-limited public API client with an injectable transport."""

    _limiter_lock = threading.Lock()
    _next_request_at = 0.0
    request_interval_seconds = 0.25
    max_rate_limit_wait_seconds = 5.0

    def __init__(
        self,
        cache_dir: Path | None = None,
        transport: Callable[[str], Mapping[str, Any] | List[Any]] | None = None,
        # 40 was sized for anonymous use (60 req/hour); it left even a
        # modest owned-repository count partial before a per-repo language
        # fetch was added. With a GITHUB_TOKEN (5000 req/hour) and the
        # per-client cooldown in app.py, 120 is still bounded but covers a
        # realistic profile end to end.
        max_requests: int = 120,
        timeout_seconds: int = 12,
        token: Optional[str] = None,
    ) -> None:
        self.cache_dir = cache_dir or Path(os.environ.get("GAA_CACHE_DIR", ".cache/github-account-analysis"))
        self.transport = transport
        self.max_requests = max_requests
        self.timeout_seconds = timeout_seconds
        # Optional: raises the GitHub REST rate limit from 60/hour (anonymous) to
        # 5000/hour. Never logged; only whether one was used is reported (see
        # report.py's config.credentials_used).
        self.token = token if token is not None else os.environ.get("GITHUB_TOKEN")
        self.requests = 0
        self.provenance: List[Dict[str, Any]] = []
        self.partial_reasons: List[str] = []

    @classmethod
    def _acquire_request_slot(cls) -> bool:
        """Reserve a process-wide request slot before touching GitHub.

        Returns False without sleeping when the pending wait (typically a
        GitHub rate-limit backoff set by _pause_for_rate_limit, which can be
        up to an hour) exceeds max_rate_limit_wait_seconds. Blocking the HTTP
        handler thread for that long only produces a proxy/browser timeout
        with a non-JSON error body; failing fast lets the caller record it as
        a partial result instead.
        """
        with cls._limiter_lock:
            now = time.monotonic()
            delay = max(0.0, cls._next_request_at - now)
            if delay > cls.max_rate_limit_wait_seconds:
                return False
            cls._next_request_at = max(now, cls._next_request_at) + cls.request_interval_seconds
        if delay:
            time.sleep(delay)
        return True

    @classmethod
    def _pause_for_rate_limit(cls, headers: Any) -> None:
        retry_after = headers.get("Retry-After") if headers else None
        reset_at = headers.get("X-RateLimit-Reset") if headers else None
        pause = 0.0
        try:
            pause = float(retry_after) if retry_after else 0.0
        except ValueError:
            pause = 0.0
        if not pause and reset_at:
            try:
                pause = max(0.0, float(reset_at) - time.time())
            except ValueError:
                pass
        if pause:
            with cls._limiter_lock:
                cls._next_request_at = max(cls._next_request_at, time.monotonic() + pause)

    def _cached_path(self, url: str) -> Path:
        return self.cache_dir / f"{hashlib.sha256(url.encode()).hexdigest()}.json"

    def get(self, endpoint: str, params: Mapping[str, Any] | None = None) -> Mapping[str, Any] | List[Any] | None:
        query = urlencode({key: value for key, value in (params or {}).items() if value is not None})
        url = f"{API_BASE}{endpoint}" + (f"?{query}" if query else "")
        cache_path = self._cached_path(url)
        if self.transport is None and cache_path.exists():
            try:
                payload = json.loads(cache_path.read_text(encoding="utf-8"))
                self.provenance.append({"url": url, "cached": True, "status": 200})
                return payload
            except (OSError, json.JSONDecodeError):
                cache_path.unlink(missing_ok=True)
        if self.requests >= self.max_requests:
            self.partial_reasons.append(f"request budget of {self.max_requests} public API calls reached")
            return None
        self.requests += 1
        if not self._acquire_request_slot():
            self.partial_reasons.append(f"{endpoint}: GitHub rate limit backoff exceeded {self.max_rate_limit_wait_seconds:.0f}s; request skipped")
            self.provenance.append({"url": url, "cached": False, "status": "rate_limited_skipped"})
            return None
        try:
            if self.transport:
                payload = self.transport(url)
            else:
                headers = {"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT}
                if self.token:
                    headers["Authorization"] = f"Bearer {self.token}"
                request = Request(url, headers=headers)
                with urlopen(request, timeout=self.timeout_seconds) as response:  # nosec B310: API_BASE is constant
                    payload = json.loads(response.read().decode("utf-8"))
            if self.transport is None:
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(json.dumps(payload), encoding="utf-8")
            self.provenance.append({"url": url, "cached": False, "status": 200})
            return payload
        except HTTPError as error:
            rate_limited = error.code == 429 or (
                error.code == 403 and error.headers.get("X-RateLimit-Remaining") == "0"
            )
            if rate_limited:
                self._pause_for_rate_limit(error.headers)
            detail = " (rate limited)" if rate_limited else ""
            self.partial_reasons.append(f"{endpoint}: GitHub returned HTTP {error.code}{detail}")
            self.provenance.append({"url": url, "cached": False, "status": error.code})
        except (URLError, OSError, json.JSONDecodeError) as error:
            self.partial_reasons.append(f"{endpoint}: request failed ({type(error).__name__})")
            self.provenance.append({"url": url, "cached": False, "status": "failed"})
        return None

    def pages(self, endpoint: str, params: Mapping[str, Any] | None = None, limit: int = 3) -> List[Mapping[str, Any]]:
        records: List[Mapping[str, Any]] = []
        for page in range(1, limit + 1):
            payload = self.get(endpoint, {**(params or {}), "per_page": 100, "page": page})
            if not isinstance(payload, list):
                break
            records.extend(item for item in payload if isinstance(item, Mapping))
            if len(payload) < 100:
                break
        else:
            self.partial_reasons.append(f"{endpoint}: pagination capped at {limit} pages")
        return records


def _in_timeframe(timestamp: str | None, since: datetime | None) -> bool:
    if not since or not timestamp:
        return True
    try:
        return datetime.fromisoformat(timestamp.replace("Z", "+00:00")) >= since
    except ValueError:
        return False


def collect_public_data(login_value: str, since: datetime | None, scope: Iterable[str], client: GitHubClient) -> Dict[str, Any]:
    """Collect only endpoint-exposed public metadata, with endpoint limitations retained."""
    login = normalize_login(login_value)
    profile = client.get(f"/users/{login}")
    if not isinstance(profile, Mapping):
        raise GitHubAPIError("GitHub profile could not be retrieved; no report was created.")
    scope_set = set(scope)
    repos = client.pages(f"/users/{login}/repos", {"type": "owner", "sort": "updated"}) if "owned" in scope_set else []
    owned_repo_names = {str(repo.get("full_name")) for repo in repos if repo.get("full_name")}
    events = client.pages(f"/users/{login}/events/public") if "contributed" in scope_set else []
    commits: List[Dict[str, Any]] = []
    pulls: List[Dict[str, Any]] = []
    repo_languages: Dict[str, Dict[str, int]] = {}
    if "owned" in scope_set:
        for repo in repos:
            name = repo.get("full_name")
            if not name:
                continue
            for summary in client.pages(f"/repos/{name}/commits", {"author": login, "since": since.isoformat() if since else None}, limit=1):
                sha = summary.get("sha")
                detail = client.get(f"/repos/{name}/commits/{sha}") if sha else None
                if isinstance(detail, Mapping):
                    commits.append({**detail, "repository": name, "ownership": "owned"})
            for pull in client.pages(f"/repos/{name}/pulls", {"state": "all", "sort": "created", "direction": "desc"}, limit=1):
                if (
                    str((pull.get("user") or {}).get("login", "")).lower() == login.lower()
                    and _in_timeframe(str(pull.get("created_at", "")), since)
                ):
                    pulls.append({**pull, "repository": name, "ownership": "owned"})
            languages = client.get(f"/repos/{name}/languages")
            if isinstance(languages, Mapping):
                repo_languages[name] = {str(key): int(value) for key, value in languages.items() if isinstance(value, (int, float))}
    if "contributed" in scope_set:
        for event in events:
            created = str(event.get("created_at", ""))
            if not _in_timeframe(created, since):
                continue
            repository = str((event.get("repo") or {}).get("name", ""))
            payload = event.get("payload") or {}
            if event.get("type") == "PushEvent":
                for entry in payload.get("commits", []) or []:
                    sha = entry.get("sha")
                    detail = client.get(f"/repos/{repository}/commits/{sha}") if repository and sha else None
                    if isinstance(detail, Mapping):
                        commits.append(
                            {
                                **detail,
                                "repository": repository,
                                "ownership": "owned" if repository in owned_repo_names else "contributed",
                                "event_limited": True,
                            }
                        )
            if (
                event.get("type") == "PullRequestEvent"
                and payload.get("action") == "opened"
                and isinstance(payload.get("pull_request"), Mapping)
                and _in_timeframe(str(payload["pull_request"].get("created_at", "")), since)
            ):
                pull = payload["pull_request"]
                ownership = "owned" if repository in owned_repo_names or str((((pull.get("base") or {}).get("repo") or {}).get("owner") or {}).get("login", "")).lower() == login.lower() else "contributed"
                pulls.append({**pull, "repository": repository, "ownership": ownership})
    if "contributed" in scope_set:
        client.partial_reasons.append("public events expose a limited recent history; contribution coverage is not a complete historical record")
    return {
        "profile": dict(profile),
        "repos": [dict(repo) for repo in repos],
        "repo_languages": repo_languages,
        "commits": commits,
        "pulls": pulls,
        "provenance": client.provenance,
        "partial_reasons": client.partial_reasons,
        "collected_at": datetime.now(timezone.utc).isoformat(),
    }
