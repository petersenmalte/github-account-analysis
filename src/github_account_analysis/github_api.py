"""Bounded anonymous GitHub REST API collector; it never clones or executes targets."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from datetime import date, datetime, timedelta, timezone
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
    request_interval_seconds = float(os.environ.get("GAA_REQUEST_INTERVAL_SECONDS", "0.1"))
    max_rate_limit_wait_seconds = 5.0

    # Defaults sized to what GitHub actually allows per hour, so a normal
    # account is analyzed completely instead of being cut off by an
    # arbitrary local cap: 60 requests/hour anonymously, 5000 with a token.
    # GAA_MAX_REQUESTS overrides both for a shared deployment.
    anonymous_request_budget = 60
    token_request_budget = 4000
    # Mutable API responses (lists, profiles, search) are re-fetched after
    # this many seconds; responses addressed by an immutable commit SHA are
    # cached indefinitely.
    default_cache_ttl_seconds = 3600

    def __init__(
        self,
        cache_dir: Path | None = None,
        transport: Callable[[str], Mapping[str, Any] | List[Any]] | None = None,
        max_requests: Optional[int] = None,
        timeout_seconds: int = 12,
        token: Optional[str] = None,
        cache_ttl_seconds: Optional[float] = None,
        max_seconds: Optional[float] = None,
    ) -> None:
        self.cache_dir = cache_dir or Path(os.environ.get("GAA_CACHE_DIR", ".cache/github-account-analysis"))
        self.transport = transport
        self.timeout_seconds = timeout_seconds
        # Optional: raises the GitHub REST rate limit from 60/hour (anonymous) to
        # 5000/hour. Never logged; only whether one was used is reported (see
        # report.py's config.credentials_used).
        self.token = token if token is not None else os.environ.get("GITHUB_TOKEN")
        if max_requests is None:
            configured = os.environ.get("GAA_MAX_REQUESTS")
            max_requests = int(configured) if configured else (
                self.token_request_budget if self.token else self.anonymous_request_budget
            )
        self.max_requests = max_requests
        self.cache_ttl_seconds = (
            cache_ttl_seconds
            if cache_ttl_seconds is not None
            else float(os.environ.get("GAA_CACHE_TTL_SECONDS", self.default_cache_ttl_seconds))
        )
        # Wall-clock budget for one collection, so a browser request cannot
        # hang indefinitely on a very large account; optional enrichment
        # phases stop first and are reported as partial.
        self.max_seconds = (
            max_seconds if max_seconds is not None else float(os.environ.get("GAA_MAX_SECONDS", "900"))
        )
        self.started_at = time.monotonic()
        self.requests = 0
        self.provenance: List[Dict[str, Any]] = []
        self.partial_reasons: List[str] = []
        # Last X-RateLimit-Remaining seen per GitHub rate-limit resource
        # ("core", "search", ...); None until a response reports it.
        self.rate_remaining: Dict[str, int] = {}
        self.rate_reset_at: Dict[str, float] = {}

    @property
    def remaining_requests(self) -> int:
        """Requests this collection may still make before a limit is reached."""
        local = self.max_requests - self.requests
        core = self.rate_remaining.get("core")
        return max(0, min(local, core) if core is not None else local)

    def time_left(self) -> float:
        return self.max_seconds - (time.monotonic() - self.started_at)

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

    @staticmethod
    def _is_immutable(endpoint: str) -> bool:
        """A single commit addressed by its full SHA never changes."""
        parts = endpoint.strip("/").split("/")
        return len(parts) == 5 and parts[0] == "repos" and parts[3] == "commits" and len(parts[4]) == 40

    @staticmethod
    def _resource(endpoint: str) -> str:
        if endpoint.startswith("/search/"):
            return "search"
        return "core"

    def _record_rate_headers(self, headers: Any) -> None:
        if not headers:
            return
        resource = headers.get("X-RateLimit-Resource") or "core"
        remaining = headers.get("X-RateLimit-Remaining")
        reset_at = headers.get("X-RateLimit-Reset")
        try:
            if remaining is not None:
                self.rate_remaining[str(resource)] = int(remaining)
            if reset_at is not None:
                self.rate_reset_at[str(resource)] = float(reset_at)
        except ValueError:
            pass

    def get(
        self,
        endpoint: str,
        params: Mapping[str, Any] | None = None,
        *,
        missing_ok: Iterable[int] = (),
    ) -> Mapping[str, Any] | List[Any] | None:
        """GET one API resource.

        HTTP statuses in missing_ok (e.g. 409 for an empty repository's
        commit list) are expected, recorded in provenance, and do not make
        the analysis partial.
        """
        query = urlencode({key: value for key, value in (params or {}).items() if value is not None})
        url = f"{API_BASE}{endpoint}" + (f"?{query}" if query else "")
        cache_path = self._cached_path(url)
        if self.transport is None and cache_path.exists():
            try:
                fresh = self._is_immutable(endpoint) or (
                    time.time() - cache_path.stat().st_mtime < self.cache_ttl_seconds
                )
                if fresh:
                    payload = json.loads(cache_path.read_text(encoding="utf-8"))
                    self.provenance.append({"url": url, "cached": True, "status": 200})
                    return payload
            except (OSError, json.JSONDecodeError):
                cache_path.unlink(missing_ok=True)
        if self.requests >= self.max_requests:
            self.partial_reasons.append(f"request budget of {self.max_requests} public API calls reached")
            return None
        resource = self._resource(endpoint)
        if self.rate_remaining.get(resource) == 0 and self.rate_reset_at.get(resource, 0) > time.time():
            self.partial_reasons.append(f"GitHub {resource} rate limit exhausted; remaining requests skipped")
            self.provenance.append({"url": url, "cached": False, "status": "rate_limited_skipped"})
            return None
        self.requests += 1
        # Only real network calls are paced; an injected (offline) transport is not.
        if self.transport is None and not self._acquire_request_slot():
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
                    self._record_rate_headers(response.headers)
                    payload = json.loads(response.read().decode("utf-8"))
            if self.transport is None:
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(json.dumps(payload), encoding="utf-8")
            self.provenance.append({"url": url, "cached": False, "status": 200})
            return payload
        except HTTPError as error:
            self._record_rate_headers(error.headers)
            if error.code in set(missing_ok):
                self.provenance.append({"url": url, "cached": False, "status": error.code})
                return None
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

    def pages(
        self,
        endpoint: str,
        params: Mapping[str, Any] | None = None,
        limit: Optional[int] = None,
        *,
        missing_ok: Iterable[int] = (),
    ) -> List[Mapping[str, Any]]:
        """Follow list pagination until the last page (or a stated cap)."""
        if limit is None:
            limit = int(os.environ.get("GAA_MAX_PAGES", "100"))
        records: List[Mapping[str, Any]] = []
        for page in range(1, limit + 1):
            payload = self.get(endpoint, {**(params or {}), "per_page": 100, "page": page}, missing_ok=missing_ok)
            if not isinstance(payload, list):
                break
            records.extend(item for item in payload if isinstance(item, Mapping))
            if len(payload) < 100:
                break
        else:
            self.partial_reasons.append(f"{endpoint}: pagination capped at {limit} pages")
        return records


SEARCH_RESULT_CAP = 1000  # GitHub search never returns more than 1000 results per query


def _search_window(
    client: GitHubClient, kind: str, qualifiers: str, date_field: str, start: date, end: date
) -> Optional[List[Mapping[str, Any]]]:
    """Return every search hit in [start, end], splitting the date range while
    a single query would exceed GitHub's 1000-result cap. None means the
    search API could not be used at all."""
    query = f"{qualifiers} {date_field}:{start.isoformat()}..{end.isoformat()}"
    endpoint = f"/search/{kind}"
    first = client.get(endpoint, {"q": query, "per_page": 100, "page": 1})
    if not isinstance(first, Mapping):
        return None
    total = int(first.get("total_count") or 0)
    if total > SEARCH_RESULT_CAP and start < end:
        middle = start + (end - start) / 2
        left = _search_window(client, kind, qualifiers, date_field, start, middle)
        right = _search_window(client, kind, qualifiers, date_field, middle + timedelta(days=1), end)
        if left is None and right is None:
            return None
        return (left or []) + (right or [])
    if total > SEARCH_RESULT_CAP:
        client.partial_reasons.append(
            f"{endpoint}: more than {SEARCH_RESULT_CAP} results on {start.isoformat()}; only the first {SEARCH_RESULT_CAP} are available"
        )
    if first.get("incomplete_results"):
        client.partial_reasons.append(f"{endpoint}: GitHub reported incomplete search results (search timed out)")
    items = [item for item in first.get("items") or [] if isinstance(item, Mapping)]
    last_page = -(-min(total, SEARCH_RESULT_CAP) // 100)
    for page in range(2, last_page + 1):
        payload = client.get(endpoint, {"q": query, "per_page": 100, "page": page})
        if not isinstance(payload, Mapping) or not payload.get("items"):
            break
        items.extend(item for item in payload["items"] if isinstance(item, Mapping))
    return items


def search_all(
    client: GitHubClient, kind: str, qualifiers: str, date_field: str, since: Optional[datetime], created_at: str | None
) -> Optional[List[Mapping[str, Any]]]:
    today = datetime.now(timezone.utc).date()
    start = since.date() if since else None
    if start is None and created_at:
        try:
            start = datetime.fromisoformat(created_at.replace("Z", "+00:00")).date()
        except ValueError:
            start = None
    return _search_window(client, kind, qualifiers, date_field, start or date(2008, 1, 1), today)


def _in_timeframe(timestamp: str | None, since: datetime | None) -> bool:
    if not since or not timestamp:
        return True
    try:
        return datetime.fromisoformat(timestamp.replace("Z", "+00:00")) >= since
    except ValueError:
        return False


def _repo_from_url(url: str) -> str:
    """https://api.github.com/repos/{owner}/{repo} -> owner/repo"""
    parts = urlparse(url).path.strip("/").split("/")
    return "/".join(parts[1:3]) if len(parts) >= 3 and parts[0] == "repos" else ""


def collect_public_data(login_value: str, since: datetime | None, scope: Iterable[str], client: GitHubClient) -> Dict[str, Any]:
    """Collect endpoint-exposed public metadata for one account.

    Collection runs in phases so that limited budgets cut optional detail,
    not coverage:

    1. complete lists: profile, every owned repository, its languages, every
       commit authored by the account on each default branch, and every
       pull request / default-branch commit GitHub search attributes to the
       account anywhere on GitHub;
    2. commits that exist only on other branches of owned repositories;
    3. per-commit diff statistics (one request per commit) for change volume.
    """
    login = normalize_login(login_value)
    profile = client.get(f"/users/{login}")
    if not isinstance(profile, Mapping):
        raise GitHubAPIError("GitHub profile could not be retrieved; no report was created.")
    login = str(profile.get("login") or login)
    scope_set = set(scope)
    since_param = since.isoformat() if since else None
    coverage: Dict[str, Any] = {
        "owned_repositories_listed": 0,
        "owned_repositories_commit_history_listed": 0,
        "empty_repositories": 0,
        "branches_scanned": 0,
        "branch_scan": "not run",
        "pull_request_source": "not run",
        "contributed_commit_source": "not run",
        "commits_found": 0,
        "commits_with_change_statistics": 0,
    }

    # Phase 1a: owned repositories (all pages; forks are included and flagged).
    repos = client.pages(f"/users/{login}/repos", {"type": "owner", "sort": "updated"}) if "owned" in scope_set else []
    coverage["owned_repositories_listed"] = len(repos)
    owned_repo_names = {str(repo.get("full_name")) for repo in repos if repo.get("full_name")}
    owned_lower = {name.lower() for name in owned_repo_names}

    def ownership_of(repository: str) -> str:
        owner = repository.split("/")[0].lower() if repository else ""
        return "owned" if repository.lower() in owned_lower or owner == login.lower() else "contributed"

    def in_scope(ownership: str) -> bool:
        return ownership in scope_set

    commits: List[Dict[str, Any]] = []
    seen_shas: set = set()
    pulls: List[Dict[str, Any]] = []
    seen_pulls: set = set()

    def add_pull(item: Mapping[str, Any], repository: str, ownership: str) -> None:
        key = (repository.lower(), item.get("number"))
        if key in seen_pulls:
            return
        seen_pulls.add(key)
        pulls.append({**item, "repository": repository, "ownership": ownership})
    repo_languages: Dict[str, Dict[str, int]] = {}

    def add_commit(item: Mapping[str, Any], repository: str, ownership: str, source: str) -> None:
        sha = str(item.get("sha") or "")
        if sha and sha in seen_shas:
            return
        if sha:
            seen_shas.add(sha)
        record = {key: value for key, value in item.items() if key != "repository"}
        commits.append({**record, "repository": repository, "ownership": ownership, "evidence_source": source})

    # Phase 1b: languages and default-branch commit history of every owned repository.
    if "owned" in scope_set:
        for repo in repos:
            name = repo.get("full_name")
            if not name:
                continue
            languages = client.get(f"/repos/{name}/languages")
            if isinstance(languages, Mapping):
                repo_languages[name] = {str(key): int(value) for key, value in languages.items() if isinstance(value, (int, float))}
            if repo.get("size") == 0:
                coverage["empty_repositories"] += 1
            before = len(client.provenance)
            listed = client.pages(
                f"/repos/{name}/commits", {"author": login, "since": since_param}, missing_ok=(409,)
            )
            statuses = [entry.get("status") for entry in client.provenance[before:]]
            if 409 in statuses:
                coverage["empty_repositories"] += 0 if repo.get("size") == 0 else 1
            if statuses and all(status in (200, 409) for status in statuses):
                coverage["owned_repositories_commit_history_listed"] += 1
            for summary in listed:
                add_commit(summary, name, "owned", "repository commit list (default branch)")

    # Phase 1c: pull requests the account opened anywhere, via search.
    pr_items = search_all(client, "issues", f"author:{login} type:pr", "created", since, profile.get("created_at"))
    if pr_items is not None:
        coverage["pull_request_source"] = "search API (all public repositories)"
        for item in pr_items:
            repository = _repo_from_url(str(item.get("repository_url", "")))
            ownership = ownership_of(repository)
            if not in_scope(ownership) or not _in_timeframe(str(item.get("created_at", "")), since):
                continue
            merged_at = (item.get("pull_request") or {}).get("merged_at")
            add_pull({**item, "merged_at": merged_at}, repository, ownership)
    elif "owned" in scope_set:
        coverage["pull_request_source"] = "per-repository pull lists (search API unavailable; other repositories not covered)"
        for repo in repos:
            name = repo.get("full_name")
            for pull in client.pages(f"/repos/{name}/pulls", {"state": "all", "sort": "created", "direction": "desc"}):
                if (
                    str((pull.get("user") or {}).get("login", "")).lower() == login.lower()
                    and _in_timeframe(str(pull.get("created_at", "")), since)
                ):
                    add_pull(pull, name, "owned")

    # Phase 1d: default-branch commits in repositories the account does not own.
    if "contributed" in scope_set:
        commit_items = search_all(client, "commits", f"author:{login}", "author-date", since, profile.get("created_at"))
        if commit_items is not None:
            coverage["contributed_commit_source"] = "search API (default branches of public repositories)"
            for item in commit_items:
                repository = str((item.get("repository") or {}).get("full_name", ""))
                ownership = ownership_of(repository)
                if in_scope(ownership):
                    add_commit(item, repository, ownership, "commit search")
        else:
            coverage["contributed_commit_source"] = "unavailable (search API failed)"

    # Phase 1e: recent public events (supplementary; payload formats vary).
    events = client.pages(f"/users/{login}/events/public", limit=3) if "contributed" in scope_set else []
    for event in events:
        created = str(event.get("created_at", ""))
        if not _in_timeframe(created, since):
            continue
        repository = str((event.get("repo") or {}).get("name", ""))
        payload = event.get("payload") or {}
        ownership = ownership_of(repository)
        if event.get("type") == "PushEvent":
            # Older payloads list commits; since late 2025 GitHub only sends
            # head/before, and those commits are covered by phases 1b-1d.
            for entry in payload.get("commits", []) or []:
                sha = entry.get("sha")
                if not (repository and sha) or sha in seen_shas:
                    continue
                detail = client.get(f"/repos/{repository}/commits/{sha}")
                if isinstance(detail, Mapping) and in_scope(ownership):
                    add_commit({**detail, "event_limited": True}, repository, ownership, "public event")
        if (
            event.get("type") == "PullRequestEvent"
            and payload.get("action") == "opened"
            and isinstance(payload.get("pull_request"), Mapping)
            and _in_timeframe(str(payload["pull_request"].get("created_at", "")), since)
        ):
            pull = payload["pull_request"]
            base_owner = str((((pull.get("base") or {}).get("repo") or {}).get("owner") or {}).get("login", ""))
            pr_ownership = "owned" if ownership == "owned" or base_owner.lower() == login.lower() else "contributed"
            if in_scope(pr_ownership):
                add_pull(pull, repository, pr_ownership)
    if "contributed" in scope_set:
        client.partial_reasons.append(
            "GitHub search only indexes default branches of public repositories; contributions on other branches of repositories the account does not own, or to private repositories, are not visible"
        )

    # Phase 2: commits that exist only on non-default branches of owned, non-fork repositories.
    if "owned" in scope_set:
        coverage["branch_scan"] = "complete"
        for repo in repos:
            name = repo.get("full_name")
            if not name or repo.get("fork") or repo.get("size") == 0:
                continue
            if client.remaining_requests <= 0 or client.time_left() <= 0:
                coverage["branch_scan"] = "stopped early (request or time budget)"
                break
            default_branch = repo.get("default_branch")
            for branch in client.pages(f"/repos/{name}/branches", missing_ok=(409,)):
                branch_name = branch.get("name")
                head = str((branch.get("commit") or {}).get("sha", ""))
                if not branch_name or branch_name == default_branch or head in seen_shas:
                    continue
                if client.remaining_requests <= 0 or client.time_left() <= 0:
                    coverage["branch_scan"] = "stopped early (request or time budget)"
                    break
                coverage["branches_scanned"] += 1
                for summary in client.pages(
                    f"/repos/{name}/commits", {"sha": branch_name, "author": login, "since": since_param}, missing_ok=(409,)
                ):
                    add_commit(summary, name, "owned", f"repository commit list (branch {branch_name})")
        if coverage["branch_scan"] != "complete":
            client.partial_reasons.append("non-default branches of owned repositories were only partly scanned (request or time budget)")

    # Phase 3: per-commit diff statistics for change volume (optional detail).
    for index, item in enumerate(commits):
        if item.get("files") is not None:
            continue
        if client.remaining_requests <= 0 or client.time_left() <= 0:
            break
        detail = client.get(f"/repos/{item['repository']}/commits/{item.get('sha')}")
        if isinstance(detail, Mapping):
            commits[index] = {**item, **{key: value for key, value in detail.items() if key != "repository"}}
    coverage["commits_found"] = len(commits)
    coverage["commits_with_change_statistics"] = sum(1 for item in commits if item.get("files") is not None)
    if coverage["commits_with_change_statistics"] < coverage["commits_found"]:
        client.partial_reasons.append(
            f"change volume measured for {coverage['commits_with_change_statistics']} of {coverage['commits_found']} commits "
            "(one API request per commit; set GITHUB_TOKEN for a higher limit)"
        )

    return {
        "profile": dict(profile),
        "repos": [dict(repo) for repo in repos],
        "repo_languages": repo_languages,
        "commits": commits,
        "pulls": pulls,
        "coverage": coverage,
        "provenance": client.provenance,
        "partial_reasons": client.partial_reasons,
        "collected_at": datetime.now(timezone.utc).isoformat(),
    }
