import unittest
from unittest.mock import patch

from github_account_analysis.app import _since, analyze


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
        client = type("StubClient", (), {"max_requests": 17})()
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
            },
            collected,
        )
