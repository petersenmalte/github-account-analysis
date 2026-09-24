import json
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from threading import Thread
from unittest.mock import patch

from github_account_analysis.app import ApplicationHandler, _since, analyze


class AppInputTests(unittest.TestCase):
    def test_timeframe_options_are_bounded_and_validate_unknown_values(self):
        self.assertIsNone(_since("all_available"))
        self.assertIsNotNone(_since("30_days"))
        with self.assertRaises(ValueError):
            _since("forever")

    def test_analyze_rejects_invalid_scope_values(self):
        with self.assertRaises(ValueError):
            analyze({"username": "octocat", "scope": ["owned", "private"]})

    def test_analyze_uses_validated_request_payload(self):
        client = type("StubClient", (), {"max_requests": 17, "token": None})()
        collected = {
            "profile": {"login": "octocat", "html_url": "https://github.com/octocat"},
            "repos": [],
            "commits": [],
            "pulls": [],
            "provenance": [],
            "partial_reasons": [],
        }
        report = {"subject": {"login": "octocat"}, "metrics": {}}

        with (
            patch("github_account_analysis.app.collect_public_data", return_value=collected) as collect,
            patch("github_account_analysis.app.build_report", return_value=report) as build,
        ):
            result = analyze(
                {"username": "https://github.com/octocat", "timeframe": "30_days", "scope": ["owned"]},
                client=client,
            )

        self.assertIs(result, report)
        collect.assert_called_once()
        self.assertEqual(collect.call_args.args[0], "octocat")
        self.assertEqual(collect.call_args.args[2], ["owned"])
        self.assertIs(collect.call_args.args[3], client)
        build.assert_called_once_with(
            {
                "username": "octocat",
                "timeframe": "30_days",
                "scope": ["owned"],
                "api_request_budget": 17,
                "credentials_used": False,
            },
            collected,
        )

    def test_analyze_reports_when_a_token_was_used(self):
        client = type("StubClient", (), {"max_requests": 40, "token": "ghp_example"})()
        collected = {
            "profile": {"login": "octocat", "html_url": "https://github.com/octocat"},
            "repos": [],
            "commits": [],
            "pulls": [],
            "provenance": [],
            "partial_reasons": [],
        }
        report = {"subject": {"login": "octocat"}, "metrics": {}}

        with (
            patch("github_account_analysis.app.collect_public_data", return_value=collected),
            patch("github_account_analysis.app.build_report", return_value=report) as build,
        ):
            analyze({"username": "octocat", "scope": ["owned"]}, client=client)

        self.assertTrue(build.call_args.args[0]["credentials_used"])


class ClientCooldownTests(unittest.TestCase):
    """A public deployment shares one GitHub API budget across every visitor."""

    def setUp(self):
        ApplicationHandler._last_request_at = {}
        ApplicationHandler.min_seconds_between_requests = 60.0
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), ApplicationHandler)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()
        ApplicationHandler.min_seconds_between_requests = 20.0

    def _post(self, path: str, body: dict) -> tuple[int, dict]:
        conn = HTTPConnection("127.0.0.1", self.server.server_address[1])
        payload = json.dumps(body).encode("utf-8")
        conn.request("POST", path, body=payload, headers={"Content-Type": "application/json"})
        response = conn.getresponse()
        status, data = response.status, json.loads(response.read())
        conn.close()
        return status, data

    def test_second_request_from_same_client_is_rejected_within_cooldown(self):
        # Fails fast on username validation, before any network call — the
        # cooldown check itself is what this test exercises, not analyze().
        first_status, _ = self._post("/api/analyze", {"username": "!!!"})
        second_status, second_body = self._post("/api/analyze", {"username": "!!!"})

        self.assertEqual(first_status, 400)
        self.assertEqual(second_status, 429)
        self.assertIn("wait", second_body["error"])

    def test_cooldown_does_not_apply_to_static_assets(self):
        first = HTTPConnection("127.0.0.1", self.server.server_address[1])
        first.request("GET", "/")
        self.assertEqual(first.getresponse().status, 200)
        first.close()

        second = HTTPConnection("127.0.0.1", self.server.server_address[1])
        second.request("GET", "/")
        self.assertEqual(second.getresponse().status, 200)
        second.close()
