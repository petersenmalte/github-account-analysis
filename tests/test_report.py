from __future__ import annotations

import importlib
import io
import json
from pathlib import Path
import unittest

import pytest
from pypdf import PdfReader

from github_account_analysis.ingest import ingest_event_file
from github_account_analysis.report import (
    build_report,
    generate_report,
    render_pdf,
    report_lines,
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


def legacy_commit(
    sha="a" * 40,
    repository="fixture/owned",
    message="Change\n\nAI-Classification: A",
    actor="alice",
    files=None,
    committer=None,
):
    return {
        "sha": sha,
        "repository": repository,
        "ownership": "owned" if repository == "fixture/owned" else "contributed",
        "author": {"login": actor, "type": "User"},
        "committer": committer or {"login": actor},
        "commit": {"message": message, "author": {"date": "2026-01-15T12:00:00Z"}},
        "files": files or [{"filename": "src/feature.py", "additions": 8, "deletions": 3}],
        "html_url": f"https://github.com/{repository}/commit/{sha}",
    }


def legacy_raw_data(**overrides):
    base = {
        "profile": {"login": "alice", "html_url": "https://github.com/alice"},
        "repos": [{"full_name": "fixture/owned", "language": "Python"}],
        "commits": [legacy_commit()],
        "pulls": [
            {
                "number": 5,
                "repository": "elsewhere/project",
                "ownership": "contributed",
                "user": {"login": "alice", "type": "User"},
                "created_at": "2026-01-16T12:00:00Z",
                "merged_at": "2026-01-17T12:00:00Z",
                "body": "",
                "html_url": "https://github.com/elsewhere/project/pull/5",
            }
        ],
        "provenance": [{"url": "https://api.github.com/users/alice", "status": 200}],
        "partial_reasons": [],
    }
    base.update(overrides)
    return base


class LegacyReportTests(unittest.TestCase):
    def report(self, **overrides):
        return build_report(
            {"username": "alice", "timeframe": "1_year", "scope": ["owned", "contributed"]},
            legacy_raw_data(**overrides),
        )

    def test_account_first_attribution_preserves_committer_ambiguity(self):
        other = legacy_commit(sha="b" * 40, actor="similar-name")
        changed_committer = legacy_commit(sha="c" * 40, committer={"login": "release-bot"})
        report = self.report(commits=[other, changed_committer])
        self.assertEqual(report["metrics"]["attributable_commits"], 1)
        self.assertIn("committer differs from author", report["artifacts"][0]["uncertainty"][0])

    def test_duplicate_fork_commit_is_excluded_with_reason(self):
        first = legacy_commit()
        mirror = legacy_commit(repository="fork/mirror")
        report = self.report(commits=[first, mirror])
        self.assertEqual(report["metrics"]["attributable_commits"], 1)
        self.assertEqual(len(report["deduplication_exclusions"]), 1)
        self.assertIn("identical commit object", report["deduplication_exclusions"][0]["reason"])

    def test_coauthors_and_squash_merge_are_not_individual_share_evidence(self):
        evidence = legacy_commit(
            message="Refactor\n\nCo-authored-by: Sam Example <sam@example.invalid>"
        )
        report = self.report(commits=[evidence])
        self.assertEqual(report["artifacts"][0]["coauthors"][0]["email"], "sam@example.invalid")
        pull = next(item for item in report["artifacts"] if item["type"] == "pull_request")
        self.assertIn("squash merge", pull["uncertainty"][0])
        self.assertIn("no individual change shares", " ".join(report["uncertainty"]))

    def test_ai_classes_unknown_and_bots_have_visible_denominators(self):
        bot_commit = legacy_commit(sha="d" * 40, message="", actor="alice")
        bot_commit["author"]["type"] = "Bot"
        unknown = legacy_commit(sha="e" * 40, message="")
        report = self.report(commits=[unknown, bot_commit], pulls=[])
        self.assertEqual(report["ai_metadata"]["denominator"], 1)
        self.assertEqual(report["ai_metadata"]["classes"]["C"]["count"], 1)
        self.assertEqual(report["ai_metadata"]["bots"], 1)

    def test_heuristic_ai_signals_are_reported_separately_from_explicit_declarations(self):
        mentioned = legacy_commit(sha="f" * 40, message="Fix bug\n\n🤖 Generated with Claude Code")
        unmentioned = legacy_commit(sha="a" * 40, message="Plain change")
        report = self.report(commits=[mentioned, unmentioned], pulls=[])
        signals = report["heuristic_ai_signals"]
        self.assertEqual(signals["denominator"], 2)
        self.assertEqual(signals["artifacts_with_any_mention"], 1)
        self.assertEqual(signals["percent_with_any_mention"], 50.0)
        self.assertEqual(signals["by_tool"]["Claude"], 1)
        self.assertIn("NOT verified code origin", signals["caveat"])
        # The explicit, strict metric is unaffected by the heuristic match.
        self.assertEqual(report["ai_metadata"]["classes"]["C"]["count"], 2)

    def test_generated_and_vendor_change_volume_is_separate(self):
        files = [
            {"filename": "src/code.py", "additions": 4, "deletions": 1},
            {"filename": "vendor/lib.js", "additions": 20, "deletions": 2},
            {"filename": "generated/schema.py", "additions": 10, "deletions": 0},
        ]
        report = self.report(commits=[legacy_commit(files=files)], pulls=[])
        volume = report["metrics"]["code_change_volume"]
        self.assertEqual((volume["added_lines"], volume["removed_lines"]), (4, 1))
        self.assertEqual(
            (
                volume["excluded_generated_vendor_added_lines"],
                volume["excluded_generated_vendor_removed_lines"],
            ),
            (30, 2),
        )

    def test_missing_quality_measurement_is_never_zero(self):
        report = self.report()
        self.assertEqual(report["quality"]["repository_state"]["status"], "not measured")
        self.assertTrue(
            all(value == "not measured" for value in report["quality"]["dimensions"].values())
        )
        self.assertIn("Representative historical states", report["quality"]["historical_view"]["reason"])

    @pytest.mark.skipif(not _renderer_available(), reason="WeasyPrint native libraries unavailable")
    def test_partial_report_and_pdf_have_selectable_source_text(self):
        report = self.report(partial_reasons=["rate limit exhausted"])
        document = render_pdf(report)
        lines = "\n".join(report_lines(report))
        self.assertTrue(report["partial"])
        self.assertIn("https://api.github.com/users/alice", lines)
        self.assertTrue(document.startswith(b"%PDF"))

        reader = PdfReader(io.BytesIO(document))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
        self.assertIn("Source links", text)
        self.assertIn("https://api.github.com/users/alice", text)
        self.assertIn("rate limit exhausted", text)

    def test_monthly_and_yearly_activity_use_artifact_dates(self):
        report = self.report()
        self.assertEqual(report["metrics"]["monthly_activity"], {"2026-01": 2})
        self.assertEqual(report["metrics"]["yearly_activity"], {"2026": 2})

    def test_duplicate_pull_events_are_not_double_counted(self):
        duplicate = legacy_raw_data()["pulls"][0]
        report = self.report(pulls=[duplicate, duplicate])
        self.assertEqual(report["metrics"]["opened_pull_requests"], 1)
        self.assertEqual(report["deduplication_exclusions"][0]["artifact"], "PR elsewhere/project#5")

    def test_languages_use_live_repository_summary_shape(self):
        report = self.report()
        self.assertEqual(
            report["metrics"]["languages_in_owned_repositories"]["repository_counts"],
            {"Python": 1},
        )
