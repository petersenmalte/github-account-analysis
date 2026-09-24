from __future__ import annotations

import json
from pathlib import Path
import urllib.request

import pytest

from github_account_analysis.cli import main
from github_account_analysis.github import PublicGitHubClient


def test_sample_requires_explicit_isolated_data_directory(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as exited:
        main(["sample", "--reports-dir", "reports", "--site-dir", "site"])

    assert exited.value.code == 2
    assert not (tmp_path / "data").exists()


def test_public_github_client_sends_configured_token_without_logging_it(monkeypatch) -> None:
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps({"id": 1}).encode("utf-8")

    def fake_urlopen(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    client = PublicGitHubClient(token="test-token")
    payload, _digest = client.get_json("https://api.github.com/repos/example/repository")

    assert payload == {"id": 1}
    assert captured["request"].get_header("Authorization") == "Bearer test-token"
