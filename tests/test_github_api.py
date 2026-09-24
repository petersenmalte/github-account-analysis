import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlparse

from github_account_analysis.github_api import GitHubClient, collect_public_data, normalize_login
from github_account_analysis.report import build_report


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
            if path == "/repos/alice/project/pulls":
                return [{"number": 3, "user": {"login": "alice"}, "created_at": "2026-01-03T00:00:00Z", "merged_at": "2026-01-04T00:00:00Z"}]
            raise AssertionError(f"Unexpected URL {url}")

        with tempfile.TemporaryDirectory() as directory:
            data = collect_public_data("alice", None, ["owned"], GitHubClient(cache_dir=Path(directory), transport=transport))
        report = build_report({"username": "alice", "scope": ["owned"]}, data)
        self.assertEqual(report["metrics"]["opened_pull_requests"], 1)
        self.assertEqual(report["metrics"]["merged_pull_requests"], 1)
        self.assertEqual(report["artifacts"][0]["ownership"], "owned")
