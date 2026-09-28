import os
import tempfile
import time
import unittest
from unittest.mock import patch
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse

from github_account_analysis.github_api import GitHubClient, collect_public_data, normalize_login
from github_account_analysis.report import build_report


class _RateLimitHeaders:
    def __init__(self, reset_at: float) -> None:
        self._reset_at = reset_at

    def get(self, key: str, default=None):
        return {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(self._reset_at)}.get(key, default)


class GitHubClientTests(unittest.TestCase):
    def test_username_and_profile_url_validation(self):
        self.assertEqual(normalize_login("octo-cat"), "octo-cat")
        self.assertEqual(normalize_login("https://github.com/octo-cat/"), "octo-cat")
        with self.assertRaises(ValueError):
            normalize_login("https://notgithub.example/octo-cat")

    def test_api_failure_is_recorded_as_partial_not_a_success(self):
        def unavailable(_):
            raise URLError("offline")

        with tempfile.TemporaryDirectory() as directory:
            client = GitHubClient(cache_dir=Path(directory), transport=unavailable)
            self.assertIsNone(client.get("/users/alice"))
        self.assertEqual(client.provenance[0]["status"], "failed")
        self.assertIn("request failed", client.partial_reasons[0])

    def test_request_budget_makes_limited_collection_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            client = GitHubClient(cache_dir=Path(directory), transport=lambda _: {}, max_requests=1)
            self.assertEqual(client.get("/users/alice"), {})
            self.assertIsNone(client.get("/users/bob"))
        self.assertIn("request budget", client.partial_reasons[0])

    def test_token_is_sent_as_a_bearer_authorization_header(self):
        seen_requests = []

        def capture(request, timeout=None):
            seen_requests.append(request)
            raise URLError("stop before any real network call")

        with tempfile.TemporaryDirectory() as directory:
            client = GitHubClient(cache_dir=Path(directory), token="ghp_example")
            with patch("github_account_analysis.github_api.urlopen", capture):
                client.get("/users/alice")
        self.assertEqual(seen_requests[0].get_header("Authorization"), "Bearer ghp_example")

    def test_no_token_means_no_authorization_header(self):
        seen_requests = []

        def capture(request, timeout=None):
            seen_requests.append(request)
            raise URLError("stop before any real network call")

        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {}, clear=False):
            os.environ.pop("GITHUB_TOKEN", None)
            client = GitHubClient(cache_dir=Path(directory))
            with patch("github_account_analysis.github_api.urlopen", capture):
                client.get("/users/alice")
        self.assertIsNone(seen_requests[0].get_header("Authorization"))

    def test_rate_limit_backoff_fails_fast_instead_of_blocking_the_handler_thread(self):
        def rate_limited(request, timeout=None):
            raise HTTPError(request.full_url, 403, "rate limited", _RateLimitHeaders(time.time() + 3600), None)

        original_next_request_at = GitHubClient._next_request_at
        GitHubClient._next_request_at = 0.0
        try:
            with tempfile.TemporaryDirectory() as directory:
                client = GitHubClient(cache_dir=Path(directory))
                with patch("github_account_analysis.github_api.urlopen", rate_limited):
                    self.assertIsNone(client.get("/users/alice"))
                    start = time.monotonic()
                    self.assertIsNone(client.get("/users/alice/repos"))
                    elapsed = time.monotonic() - start
            self.assertLess(elapsed, 1.0, "a hit rate-limit reset an hour out must not block the request thread")
            self.assertTrue(
                any("backoff exceeded" in reason or "rate limit exhausted" in reason for reason in client.partial_reasons)
            )
        finally:
            GitHubClient._next_request_at = original_next_request_at

    def test_default_budget_follows_githubs_hourly_limit(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {}, clear=False):
            os.environ.pop("GAA_MAX_REQUESTS", None)
            self.assertEqual(GitHubClient(cache_dir=Path(directory), token="").max_requests, 60)
            self.assertGreaterEqual(GitHubClient(cache_dir=Path(directory), token="ghp_example").max_requests, 1000)

    def test_expected_missing_status_is_not_partial(self):
        def empty_repository(request, timeout=None):
            raise HTTPError(request.full_url, 409, "Git Repository is empty.", {}, None)

        with tempfile.TemporaryDirectory() as directory:
            client = GitHubClient(cache_dir=Path(directory), token="")
            with patch("github_account_analysis.github_api.urlopen", empty_repository):
                self.assertIsNone(client.get("/repos/alice/empty/commits", missing_ok=(409,)))
        self.assertEqual(client.partial_reasons, [])
        self.assertEqual(client.provenance[0]["status"], 409)

    def test_mutable_cache_entries_expire_but_commit_details_do_not(self):
        calls = []

        def fetch(request, timeout=None):
            calls.append(request.full_url)

            class Response:
                headers = {}

                def read(self):
                    return b"{}"

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return False

            return Response()

        with tempfile.TemporaryDirectory() as directory:
            client = GitHubClient(cache_dir=Path(directory), token="", cache_ttl_seconds=0)
            with patch("github_account_analysis.github_api.urlopen", fetch):
                client.get("/users/alice/repos")
                client.get("/users/alice/repos")
                client.get(f"/repos/alice/p/commits/{'a' * 40}")
                client.get(f"/repos/alice/p/commits/{'a' * 40}")
        self.assertEqual(len(calls), 3)

    def test_injected_transport_never_reads_or_writes_default_cache(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {"GAA_CACHE_DIR": directory}):
            client = GitHubClient(transport=lambda _: {"fixture": True})
            self.assertEqual(client.get("/users/alice"), {"fixture": True})
            self.assertEqual(list(Path(directory).glob("*.json")), [])

    def test_contribution_events_verify_commits_and_only_count_opened_prs(self):
        def transport(url):
            path = urlparse(url).path
            if path == "/users/alice":
                return {"login": "alice", "html_url": "https://github.com/alice"}
            if path == "/users/alice/events/public":
                pull = {"number": 9, "user": {"login": "alice"}, "created_at": "2026-01-02T00:00:00Z", "merged_at": None}
                return [
                    {"type": "PushEvent", "created_at": "2026-01-03T00:00:00Z", "repo": {"name": "elsewhere/project"}, "payload": {"commits": [{"sha": "a" * 40}]}},
                    {"type": "PullRequestEvent", "created_at": "2026-01-02T00:00:00Z", "repo": {"name": "elsewhere/project"}, "payload": {"action": "opened", "pull_request": pull}},
                    {"type": "PullRequestEvent", "created_at": "2026-01-04T00:00:00Z", "repo": {"name": "elsewhere/project"}, "payload": {"action": "synchronize", "pull_request": pull}},
                ]
            if path.startswith("/search/"):
                return {"total_count": 0, "incomplete_results": False, "items": []}
            if path.endswith(f"/commits/{'a' * 40}"):
                return {"sha": "a" * 40, "author": {"login": "other-person"}, "commit": {"message": "", "author": {"date": "2026-01-03T00:00:00Z"}}}
            raise AssertionError(f"Unexpected URL {url}")

        with tempfile.TemporaryDirectory() as directory:
            data = collect_public_data("alice", None, ["contributed"], GitHubClient(cache_dir=Path(directory), transport=transport))
        report = build_report({"username": "alice", "scope": ["contributed"]}, data)
        self.assertEqual(report["metrics"]["attributable_commits"], 0)
        self.assertEqual(report["metrics"]["opened_pull_requests"], 1)

    def test_owned_scope_collects_attributable_pull_requests(self):
        def transport(url):
            path = urlparse(url).path
            if path == "/users/alice":
                return {"login": "alice", "html_url": "https://github.com/alice"}
            if path == "/users/alice/repos":
                return [{"full_name": "alice/project", "language": "Python"}]
            if path == "/repos/alice/project/commits":
                return []
            if path == "/search/issues":
                return {
                    "total_count": 1,
                    "incomplete_results": False,
                    "items": [
                        {
                            "number": 3,
                            "user": {"login": "alice"},
                            "created_at": "2026-01-03T00:00:00Z",
                            "repository_url": "https://api.github.com/repos/alice/project",
                            "pull_request": {"merged_at": "2026-01-04T00:00:00Z"},
                        }
                    ],
                }
            if path == "/repos/alice/project/branches":
                return [{"name": "main", "commit": {"sha": "b" * 40}}]
            if path == "/repos/alice/project/languages":
                return {"Python": 1200, "Shell": 300}
            raise AssertionError(f"Unexpected URL {url}")

        with tempfile.TemporaryDirectory() as directory:
            data = collect_public_data("alice", None, ["owned"], GitHubClient(cache_dir=Path(directory), transport=transport))
        report = build_report({"username": "alice", "scope": ["owned"]}, data)
        self.assertEqual(report["metrics"]["opened_pull_requests"], 1)
        self.assertEqual(report["metrics"]["merged_pull_requests"], 1)
        self.assertEqual(report["artifacts"][0]["ownership"], "owned")
        self.assertEqual(data["repo_languages"], {"alice/project": {"Python": 1200, "Shell": 300}})
        distribution = {
            row["language"]: row["percent"]
            for row in report["metrics"]["languages_in_owned_repositories"]["byte_weighted_distribution"]
        }
        self.assertEqual(distribution, {"Python": 80.0, "Shell": 20.0})


def _commit(sha, login="alice", date="2026-01-03T00:00:00Z", message="work"):
    return {"sha": sha, "author": {"login": login}, "commit": {"message": message, "author": {"date": date}}}


class CompleteCollectionTests(unittest.TestCase):
    """Regression tests: the collector must not silently analyze a subset."""

    def _collect(self, transport, scope=("owned", "contributed"), **client_kwargs):
        with tempfile.TemporaryDirectory() as directory:
            client = GitHubClient(cache_dir=Path(directory), transport=transport, token="", **client_kwargs)
            data = collect_public_data("alice", None, list(scope), client)
        return data, build_report({"username": "alice", "scope": list(scope)}, data)

    def test_every_commit_page_of_every_repository_is_counted_even_without_detail_budget(self):
        repos = [{"full_name": f"alice/r{i}", "default_branch": "main", "size": 10} for i in range(12)]
        history = {f"alice/r{i}": [_commit(f"{i:02d}{n:038d}") for n in range(230)] for i in range(12)}

        def transport(url):
            parsed = urlparse(url)
            path, query = parsed.path, dict(pair.split("=", 1) for pair in parsed.query.split("&") if pair)
            if path == "/users/alice":
                return {"login": "alice", "created_at": "2020-01-01T00:00:00Z"}
            if path == "/users/alice/repos":
                return repos if query.get("page") == "1" else []
            if path.startswith("/search/"):
                return {"total_count": 0, "incomplete_results": False, "items": []}
            if path.endswith("/languages"):
                return {"Python": 10}
            if path.endswith("/branches"):
                return [{"name": "main", "commit": {"sha": "f" * 40}}]
            if path.endswith("/commits"):
                name = "/".join(path.split("/")[2:4])
                page = int(query["page"])
                return history[name][(page - 1) * 100 : page * 100]
            if path.startswith("/search/"):
                return {"total_count": 0, "incomplete_results": False, "items": []}
            if path == "/users/alice/events/public":
                return []
            if "/commits/" in path:
                return {"sha": path.rsplit("/", 1)[1], "author": {"login": "alice"}, "files": []}
            raise AssertionError(url)

        data, report = self._collect(transport, max_requests=200)
        self.assertEqual(report["metrics"]["attributable_commits"], 12 * 230)
        self.assertEqual(data["coverage"]["owned_repositories_commit_history_listed"], 12)
        self.assertLess(data["coverage"]["commits_with_change_statistics"], 12 * 230)
        self.assertTrue(any("change volume measured for" in reason for reason in report["partial_reasons"]))

    def test_search_splits_date_ranges_beyond_the_1000_result_cap(self):
        seen_queries = []

        def transport(url):
            parsed = urlparse(url)
            path = parsed.path
            if path == "/users/alice":
                return {"login": "alice", "created_at": "2026-01-01T00:00:00Z"}
            if path == "/search/issues":
                from urllib.parse import parse_qs

                query = parse_qs(parsed.query)["q"][0]
                seen_queries.append(query)
                window = query.rsplit("created:", 1)[1]
                start, end = window.split("..")
                if start == "2026-01-01" and end != "2026-01-01" and len(seen_queries) == 1:
                    return {"total_count": 1500, "items": []}
                number = len(seen_queries)
                return {
                    "total_count": 1,
                    "items": [
                        {
                            "number": number,
                            "user": {"login": "alice"},
                            "created_at": f"{start}T00:00:00Z",
                            "repository_url": "https://api.github.com/repos/other/project",
                            "pull_request": {"merged_at": None},
                        }
                    ],
                }
            if path == "/search/commits":
                return {"total_count": 0, "items": []}
            if path == "/users/alice/events/public":
                return []
            raise AssertionError(url)

        data, report = self._collect(transport, scope=("contributed",))
        self.assertEqual(len(seen_queries), 3)
        self.assertEqual(report["metrics"]["opened_pull_requests"], 2)
        self.assertEqual(report["metrics"]["contributed_repositories"], 1)

    def test_commits_in_other_repositories_come_from_commit_search(self):
        def transport(url):
            path = urlparse(url).path
            if path == "/users/alice":
                return {"login": "alice", "created_at": "2026-01-01T00:00:00Z"}
            if path == "/search/issues":
                return {"total_count": 0, "items": []}
            if path == "/search/commits":
                item = _commit("c" * 40)
                item["repository"] = {"full_name": "upstream/tool"}
                return {"total_count": 1, "items": [item]}
            if path == "/users/alice/events/public":
                return []
            if path == f"/repos/upstream/tool/commits/{'c' * 40}":
                return {**_commit("c" * 40), "files": [{"filename": "a.py", "additions": 4, "deletions": 1}]}
            raise AssertionError(url)

        data, report = self._collect(transport, scope=("contributed",))
        self.assertEqual(report["metrics"]["attributable_commits"], 1)
        self.assertEqual(report["metrics"]["contributed_repositories"], 1)
        self.assertEqual(report["metrics"]["code_change_volume"]["added_lines"], 4)

    def test_pull_requests_fall_back_to_repository_lists_when_search_fails(self):
        def transport(url):
            path = urlparse(url).path
            if path == "/users/alice":
                return {"login": "alice"}
            if path == "/users/alice/repos":
                return [{"full_name": "alice/project", "default_branch": "main", "size": 1}]
            if path.startswith("/search/"):
                raise URLError("search unavailable")
            if path == "/repos/alice/project/languages":
                return {}
            if path == "/repos/alice/project/commits":
                return []
            if path == "/repos/alice/project/branches":
                return []
            if path == "/repos/alice/project/pulls":
                return [{"number": 1, "user": {"login": "alice"}, "created_at": "2026-01-03T00:00:00Z"}]
            raise AssertionError(url)

        data, report = self._collect(transport, scope=("owned",))
        self.assertEqual(report["metrics"]["opened_pull_requests"], 1)
        self.assertIn("per-repository", data["coverage"]["pull_request_source"])

    def test_commits_only_on_feature_branches_are_found(self):
        def transport(url):
            parsed = urlparse(url)
            path = parsed.path
            if path == "/users/alice":
                return {"login": "alice"}
            if path == "/users/alice/repos":
                return [{"full_name": "alice/project", "default_branch": "main", "size": 1}]
            if path.startswith("/search/"):
                return {"total_count": 0, "items": []}
            if path == "/repos/alice/project/languages":
                return {}
            if path == "/repos/alice/project/branches":
                return [{"name": "main", "commit": {"sha": "1" * 40}}, {"name": "feature", "commit": {"sha": "2" * 40}}]
            if path == "/repos/alice/project/commits":
                if "sha=feature" in parsed.query:
                    return [_commit("1" * 40), _commit("2" * 40)]
                return [_commit("1" * 40)]
            if "/commits/" in path:
                return {**_commit(path.rsplit("/", 1)[1]), "files": []}
            raise AssertionError(url)

        data, report = self._collect(transport, scope=("owned",))
        self.assertEqual(report["metrics"]["attributable_commits"], 2)
        self.assertEqual(data["coverage"]["branches_scanned"], 1)

    def test_owned_repositories_report_every_author_without_crediting_the_account(self):
        def git_commit(sha, login, name, email):
            return {
                "sha": sha,
                "author": {"login": login, "type": "Bot" if login and login.endswith("[bot]") else "User"} if login else None,
                "commit": {"message": "change", "author": {"name": name, "email": email, "date": "2026-01-03T00:00:00Z"}},
            }

        history = [
            git_commit("1" * 40, "alice", "Alice", "alice@example.test"),
            git_commit("2" * 40, None, "Claude", "noreply@anthropic.com"),
            git_commit("3" * 40, "Copilot", "copilot-swe-agent[bot]", "198982749+Copilot@users.noreply.github.com"),
            git_commit("4" * 40, "github-actions[bot]", "github-actions[bot]", "actions@example.test"),
            git_commit("5" * 40, None, "Alice Laptop", "alice@laptop.local"),
            git_commit("6" * 40, "bob", "Bob", "bob@example.test"),
        ]
        seen_params = []

        def transport(url):
            parsed = urlparse(url)
            path = parsed.path
            if path == "/users/alice":
                return {"login": "alice"}
            if path == "/users/alice/repos":
                return [{"full_name": "alice/project", "default_branch": "main", "size": 1}]
            if path.startswith("/search/"):
                return {"total_count": 0, "items": []}
            if path == "/repos/alice/project/languages":
                return {}
            if path == "/repos/alice/project/branches":
                return []
            if path == "/repos/alice/project/commits":
                seen_params.append(parsed.query)
                return history
            if "/commits/" in path:
                return {**history[0], "files": []}
            raise AssertionError(url)

        data, report = self._collect(transport, scope=("owned",))
        self.assertNotIn("author=", seen_params[0])
        self.assertEqual(report["metrics"]["attributable_commits"], 1)
        authorship = report["metrics"]["owned_repository_authorship"]
        self.assertEqual(authorship["total_commits"], 6)
        counts = {label: row["count"] for label, row in authorship["by_category"].items()}
        self.assertEqual(
            counts,
            {
                "AI coding agent identity (heuristic)": 2,
                "this account": 1,
                "bot": 1,
                "git identity not linked to a GitHub account": 1,
                "other GitHub account": 1,
            },
        )
        self.assertEqual(report["metrics"]["per_repository"][0]["all_author_commits"], 6)
        self.assertEqual(data["coverage"]["commits_with_change_statistics"], 1)
