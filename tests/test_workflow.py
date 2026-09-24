from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_workflow_has_bounded_download_and_persistent_failure_ingest_path() -> None:
    workflow = (ROOT / ".github" / "workflows" / "collect-and-publish.yml").read_text(
        encoding="utf-8"
    )

    assert 'cron: "17 3 * * *"' in workflow
    assert "cancel-in-progress: false" in workflow
    assert "--max-filesize 262144000" in workflow
    assert "download_status" in workflow
    assert "--panel-repositories-only || true" in workflow
    assert "validate-report --pdf reports/latest/report.pdf" in workflow
