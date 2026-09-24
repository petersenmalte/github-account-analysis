import unittest

from github_account_analysis.report import build_report, render_pdf, report_lines


def commit(sha="a" * 40, repository="fixture/owned", message="Change\n\nAI-Classification: A", actor="alice", files=None, committer=None):
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


def raw_data(**overrides):
    base = {
        "profile": {"login": "alice", "html_url": "https://github.com/alice"},
        "repos": [{"full_name": "fixture/owned", "language": "Python"}],
        "commits": [commit()],
        "pulls": [{
            "number": 5, "repository": "elsewhere/project", "ownership": "contributed",
            "user": {"login": "alice", "type": "User"}, "created_at": "2026-01-16T12:00:00Z",
            "merged_at": "2026-01-17T12:00:00Z", "body": "", "html_url": "https://github.com/elsewhere/project/pull/5",
        }],
        "provenance": [{"url": "https://api.github.com/users/alice", "status": 200}],
        "partial_reasons": [],
    }
    base.update(overrides)
    return base


class ReportTests(unittest.TestCase):
    def report(self, **overrides):
        return build_report({"username": "alice", "timeframe": "1_year", "scope": ["owned", "contributed"]}, raw_data(**overrides))

    def test_account_first_attribution_preserves_committer_ambiguity(self):
        other = commit(sha="b" * 40, actor="similar-name")
        changed_committer = commit(sha="c" * 40, committer={"login": "release-bot"})
        report = self.report(commits=[other, changed_committer])
        self.assertEqual(report["metrics"]["attributable_commits"], 1)
        self.assertIn("committer differs from author", report["artifacts"][0]["uncertainty"][0])

    def test_duplicate_fork_commit_is_excluded_with_reason(self):
        first = commit()
        mirror = commit(repository="fork/mirror")
        report = self.report(commits=[first, mirror])
        self.assertEqual(report["metrics"]["attributable_commits"], 1)
        self.assertEqual(len(report["deduplication_exclusions"]), 1)
        self.assertIn("identical commit object", report["deduplication_exclusions"][0]["reason"])

    def test_coauthors_and_squash_merge_are_not_individual_share_evidence(self):
        evidence = commit(message="Refactor\n\nCo-authored-by: Sam Example <sam@example.invalid>")
        report = self.report(commits=[evidence])
        self.assertEqual(report["artifacts"][0]["coauthors"][0]["email"], "sam@example.invalid")
        pull = next(item for item in report["artifacts"] if item["type"] == "pull_request")
        self.assertIn("squash merge", pull["uncertainty"][0])
        self.assertIn("no individual change shares", " ".join(report["uncertainty"]))

    def test_ai_classes_unknown_and_bots_have_visible_denominators(self):
        bot_commit = commit(sha="d" * 40, message="", actor="alice")
        bot_commit["author"]["type"] = "Bot"
        unknown = commit(sha="e" * 40, message="")
        report = self.report(commits=[unknown, bot_commit], pulls=[])
        self.assertEqual(report["ai_metadata"]["denominator"], 1)
        self.assertEqual(report["ai_metadata"]["classes"]["C"]["count"], 1)
        self.assertEqual(report["ai_metadata"]["bots"], 1)

    def test_generated_and_vendor_change_volume_is_separate(self):
        files = [
            {"filename": "src/code.py", "additions": 4, "deletions": 1},
            {"filename": "vendor/lib.js", "additions": 20, "deletions": 2},
            {"filename": "generated/schema.py", "additions": 10, "deletions": 0},
        ]
        report = self.report(commits=[commit(files=files)], pulls=[])
        volume = report["metrics"]["code_change_volume"]
        self.assertEqual((volume["added_lines"], volume["removed_lines"]), (4, 1))
        self.assertEqual((volume["excluded_generated_vendor_added_lines"], volume["excluded_generated_vendor_removed_lines"]), (30, 2))

    def test_missing_quality_measurement_is_never_zero(self):
        report = self.report()
        self.assertEqual(report["quality"]["repository_state"]["status"], "not measured")
        self.assertTrue(all(value == "not measured" for value in report["quality"]["dimensions"].values()))
        self.assertIn("Representative historical states", report["quality"]["historical_view"]["reason"])

    def test_partial_report_and_pdf_have_selectable_source_text(self):
        report = self.report(partial_reasons=["rate limit exhausted"])
        document = render_pdf(report)
        lines = "\n".join(report_lines(report))
        self.assertTrue(report["partial"])
        self.assertIn("https://api.github.com/users/alice", lines)
        self.assertTrue(document.startswith(b"%PDF-1.4"))
        self.assertIn(b"Source links", document)
        self.assertIn(b"https://api.github.com/users/alice", document)

    def test_monthly_and_yearly_activity_use_artifact_dates(self):
        report = self.report()
        self.assertEqual(report["metrics"]["monthly_activity"], {"2026-01": 2})
        self.assertEqual(report["metrics"]["yearly_activity"], {"2026": 2})

    def test_duplicate_pull_events_are_not_double_counted(self):
        duplicate = raw_data()["pulls"][0]
        report = self.report(pulls=[duplicate, duplicate])
        self.assertEqual(report["metrics"]["opened_pull_requests"], 1)
        self.assertEqual(report["deduplication_exclusions"][0]["artifact"], "PR elsewhere/project#5")

    def test_languages_use_live_repository_summary_shape(self):
        report = self.report()
        self.assertEqual(report["metrics"]["languages_in_owned_repositories"]["repository_counts"], {"Python": 1})
