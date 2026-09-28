from __future__ import annotations

import json
from pathlib import Path
from urllib.error import URLError
import urllib.request

import pytest

from github_account_analysis.cli import main
from github_account_analysis.github import CollectionError, PublicGitHubClient


def test_sample_requires_explicit_isolated_data_directory(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as exited:
        main(["sample", "--reports-dir", "reports", "--site-dir", "site"])

    assert exited.value.code == 2
    assert not (tmp_path / "data").exists()


def test_public_github_client_sends_configured_token_without_logging_it(monkeypatch) -> None:
    captured = {}

    def fake_urlopen(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        raise URLError("offline")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    client = PublicGitHubClient(token="test-token")

    with pytest.raises(CollectionError) as error:
        client.get_json("https://api.github.com/repos/example/repository")

    authorization = captured["request"].get_header("Authorization")
    assert authorization is not None
    assert authorization.startswith("Bearer ")
    assert authorization.endswith("test-token")
    assert "test-token" not in str(error.value)


def test_analyze_profile_command_writes_json(tmp_path, monkeypatch, capsys):
    from github_account_analysis import cli

    report = {
        "subject": {"login": "octocat"},
        "metrics": {
            "attributable_commits": 3,
            "opened_pull_requests": 1,
            "merged_pull_requests": 1,
            "owned_repositories": 2,
            "contributed_repositories": 0,
        },
        "coverage": {"commits_found": 3},
        "partial_reasons": [],
    }
    seen = {}

    def fake_analyze(payload, client=None):
        seen.update(payload)
        return report

    monkeypatch.setattr("github_account_analysis.app.analyze", fake_analyze)
    output = tmp_path / "report.json"
    assert cli.main(["analyze-profile", "octocat", "--scope", "owned", "--json", str(output)]) == 0
    assert seen == {"username": "octocat", "timeframe": "all_available", "scope": ["owned"]}
    assert json.loads(output.read_text())["subject"]["login"] == "octocat"
    assert '"attributable_commits": 3' in capsys.readouterr().out
