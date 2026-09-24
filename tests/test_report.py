from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

from github_account_analysis.ingest import ingest_event_file
from github_account_analysis.report import (
    generate_report,
    validate_pdf,
    write_report_failure_status,
)
from github_account_analysis.utils import canonical_json, sha256_text, utc_now_iso

from .conftest import commit, push_event, write_events


def _renderer_available() -> bool:
    try:
        importlib.import_module("weasyprint")
    except (ImportError, OSError):
        return False
    return True


def _seed_panel(store) -> None:
    fixture = {
        "selection_method": "test-panel",
        "repositories": ["https://github.com/example/repository"],
    }
    store.persist_panel(
        source={
            "source_id": "panel:" + sha256_text(canonical_json(fixture)),
            "source_url": "https://api.github.com/",
            "content_sha256": sha256_text(canonical_json(fixture)),
            "observed_at_utc": utc_now_iso(),
            "config_json": canonical_json(fixture),
        },
        repositories=[
            {
                "repository_key": "example/repository",
                "repository_id": 1,
                "full_name": "example/repository",
                "html_url": "https://github.com/example/repository",
                "is_fork": False,
                "is_archived": False,
                "pushed_at_utc": None,
                "languages": {"Python": 100},
            }
        ],
    )


@pytest.mark.skipif(not _renderer_available(), reason="WeasyPrint native libraries unavailable")
def test_generated_pdf_has_selectable_overview_only_first_page_and_links(store, rules, tmp_path: Path) -> None:
    _seed_panel(store)
    events = write_events(
        tmp_path / "events.jsonl",
        [
            push_event(
                event_id="one",
                repository="example/repository",
                repository_id=1,
                commits=[commit("a" * 40, "A\n\nAI-Assisted: yes\nAI-Tools: GitHub Copilot")],
            )
        ],
    )
    ingest_event_file(
        store,
        events,
        source_url="https://example.test/events",
        window_start_utc="2025-02-01T00:00:00Z",
        window_end_utc="2025-02-01T01:00:00Z",
        rules=rules,
    )
    reports = tmp_path / "reports"
    site = tmp_path / "site"
    manifest = generate_report(store, reports_dir=reports, site_dir=site)
    validation = validate_pdf(reports / "latest" / "report.pdf")

    assert manifest["pdf_validation"]["page_count"] >= 2
    assert validation["uri_link_count"] > 0
    assert (site / "index.html").is_file()
    assert (site / "report-manifest.json").is_file()


def test_failed_report_status_keeps_last_valid_report_reference(tmp_path: Path) -> None:
    reports = tmp_path / "reports"
    latest = reports / "latest"
    latest.mkdir(parents=True)
    (latest / "report.pdf").write_bytes(b"prior report")

    write_report_failure_status(reports, RuntimeError("renderer unavailable"))

    status = json.loads((reports / "status.json").read_text(encoding="utf-8"))
    assert status["status"] == "failed"
    assert status["last_valid_report"].endswith("reports/latest/report.pdf")
    assert (latest / "report.pdf").read_bytes() == b"prior report"
