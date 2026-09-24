from __future__ import annotations

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
