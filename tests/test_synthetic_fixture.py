from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_bundled_fixture_is_explicitly_synthetic_and_contains_expected_boundaries() -> None:
    fixture_dir = ROOT / "examples" / "synthetic"
    documentation = (fixture_dir / "README.md").read_text(encoding="utf-8")
    events = (fixture_dir / "events.jsonl").read_text(encoding="utf-8")
    repositories = json.loads((fixture_dir / "repositories.json").read_text(encoding="utf-8"))

    assert "synthetic" in documentation.lower()
    assert "AI-Assisted: yes" in events
    assert "release[bot]" in events
    assert len(repositories["repositories"]) == 3
