"""Command-line interface for bounded collection, ingestion, and reporting."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Dict, List, Optional

from .attribution import load_rules
from .github import CollectionError, collect_panel
from .ingest import EventIngestError, ingest_event_file
from .loc import LocMeasurementError, measure_loc
from .report import ReportError, generate_report, validate_pdf
from .storage import AnalyticsStore
from .utils import canonical_json, sha256_text, utc_now_iso


def _print(payload: Dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def _sample(store: AnalyticsStore, arguments: argparse.Namespace) -> Dict[str, Any]:
    fixture_dir = arguments.fixture_dir
    repositories_path = fixture_dir / "repositories.json"
    with repositories_path.open(encoding="utf-8") as handle:
        fixture = json.load(handle)
    content_sha256 = sha256_text(canonical_json(fixture))
    source_id = f"panel:synthetic:{content_sha256}"
    panel_changed = store.persist_panel(
        source={
            "source_id": source_id,
            "source_url": "synthetic://bundled-fixture/repository-panel",
            "content_sha256": content_sha256,
            "observed_at_utc": utc_now_iso(),
            "config_json": canonical_json(fixture["selection_frame"]),
        },
        repositories=fixture["repositories"],
    )
    rules = load_rules(arguments.rules)
    events = ingest_event_file(
        store,
        fixture_dir / "events.jsonl",
        source_url="synthetic://bundled-fixture/events.jsonl",
        window_start_utc="2025-02-01T00:00:00Z",
        window_end_utc="2025-02-01T01:00:00Z",
        rules=rules,
    )
    manifest = generate_report(
        store, reports_dir=arguments.reports_dir, site_dir=arguments.site_dir
    )
    return {
        "status": "succeeded",
        "synthetic": True,
        "panel_changed": panel_changed,
        "event_ingest": events,
        "report_manifest": str(arguments.reports_dir / "latest" / "report-manifest.json"),
        "report_generated_at_utc": manifest["generated_at_utc"],
    }


def _analyze_profile(arguments: argparse.Namespace) -> int:
    from .app import analyze
    from .github_api import GitHubAPIError, GitHubClient
    from .report import render_pdf

    try:
        report = analyze(
            {
                "username": arguments.username,
                "timeframe": arguments.timeframe,
                "scope": arguments.scope or ["owned", "contributed"],
            },
            client=GitHubClient(max_requests=arguments.max_requests),
        )
        if arguments.json:
            arguments.json.write_text(json.dumps(report, indent=2), encoding="utf-8")
        if arguments.pdf:
            arguments.pdf.write_bytes(render_pdf(report))
    except (GitHubAPIError, ReportError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    _print(
        {
            "subject": report["subject"]["login"],
            "metrics": {
                key: report["metrics"][key]
                for key in (
                    "attributable_commits",
                    "opened_pull_requests",
                    "merged_pull_requests",
                    "owned_repositories",
                    "contributed_repositories",
                )
            },
            "coverage": report.get("coverage", {}),
            "partial_reasons": report["partial_reasons"],
            "json": str(arguments.json) if arguments.json else None,
            "pdf": str(arguments.pdf) if arguments.pdf else None,
        }
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="github-account-analysis",
        description="Bounded, reproducible public GitHub analysis.",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="Durable DuckDB and Parquet directory (required for synthetic sample output).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    panel = subparsers.add_parser("collect-panel", help="Collect configured public repository metadata.")
    panel.add_argument("--config", type=Path, default=Path("config/repository-panel.json"))

    events = subparsers.add_parser("ingest-events", help="Ingest local GH Archive-like NDJSON or NDJSON.gz.")
    events.add_argument("--input", type=Path, required=True)
    events.add_argument("--source-url", required=True)
    events.add_argument("--window-start-utc", required=True)
    events.add_argument("--window-end-utc", required=True)
    events.add_argument("--rules", type=Path, default=Path("config/attribution-rules.v1.json"))
    events.add_argument("--max-commits", type=int, default=50_000)
    events.add_argument("--max-compressed-bytes", type=int, default=250 * 1024 * 1024)
    events.add_argument("--max-decompressed-bytes", type=int, default=1024 * 1024 * 1024)
    events.add_argument("--max-records", type=int, default=1_000_000)
    events.add_argument("--max-line-bytes", type=int, default=4 * 1024 * 1024)
    events.add_argument(
        "--repository-filter",
        action="append",
        default=[],
        help="Limit observed event commits to an explicit repository name; repeat as needed.",
    )
    events.add_argument(
        "--panel-repositories-only",
        action="store_true",
        help="Use the latest successful repository panel as the explicit event filter.",
    )

    report = subparsers.add_parser("report", help="Generate and validate HTML, PDF, and Pages site.")
    report.add_argument("--reports-dir", type=Path, default=None)
    report.add_argument("--site-dir", type=Path, default=None)

    check = subparsers.add_parser("validate-report", help="Run structural PDF checks.")
    check.add_argument("--pdf", type=Path, default=Path("reports/latest/report.pdf"))

    sample = subparsers.add_parser("sample", help="Generate explicitly synthetic demonstration outputs.")
    sample.add_argument("--fixture-dir", type=Path, default=Path("examples/synthetic"))
    sample.add_argument("--rules", type=Path, default=Path("config/attribution-rules.v1.json"))
    sample.add_argument("--reports-dir", type=Path, required=True)
    sample.add_argument("--site-dir", type=Path, required=True)

    profile = subparsers.add_parser(
        "analyze-profile",
        help="Analyze one public GitHub account (same report as the web UI) without an HTTP timeout.",
    )
    profile.add_argument("username", help="GitHub username or https://github.com/<username> URL")
    profile.add_argument(
        "--timeframe", default="all_available", choices=["30_days", "90_days", "1_year", "all_available"]
    )
    profile.add_argument(
        "--scope", action="append", choices=["owned", "contributed"], help="Repeat for both (default: both)."
    )
    profile.add_argument("--json", type=Path, default=None, help="Write the machine-readable report here.")
    profile.add_argument("--pdf", type=Path, default=None, help="Also render the PDF report here.")
    profile.add_argument("--max-requests", type=int, default=None, help="Override the API request budget.")

    loc = subparsers.add_parser("loc", help="Optionally measure local checkout LOC with cloc.")
    loc.add_argument("--repository-dir", type=Path, required=True)
    loc.add_argument("--maximum-files", type=int, default=20_000)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    if arguments.command == "sample" and arguments.data_dir is None:
        parser.error("sample requires an explicit --data-dir to keep synthetic state isolated")
    if arguments.data_dir is None:
        arguments.data_dir = Path("data")
    if arguments.command == "report":
        if arguments.reports_dir is None:
            arguments.reports_dir = Path("reports")
        if arguments.site_dir is None:
            arguments.site_dir = Path("site")
    if arguments.command == "analyze-profile":
        return _analyze_profile(arguments)
    store = AnalyticsStore(arguments.data_dir)
    try:
        if arguments.command == "collect-panel":
            _print(collect_panel(store, arguments.config))
        elif arguments.command == "ingest-events":
            repository_filter = list(arguments.repository_filter)
            if arguments.panel_repositories_only:
                panel_source_id = store.latest_panel_source_id()
                if panel_source_id is None:
                    raise ValueError("--panel-repositories-only requires a successful panel collection")
                repository_filter.extend(
                    row["repository_key"]
                    for row in store.query(
                        "SELECT repository_key FROM panel_repositories WHERE source_id = ?",
                        [panel_source_id],
                    )
                )
            _print(
                ingest_event_file(
                    store,
                    arguments.input,
                    source_url=arguments.source_url,
                    window_start_utc=arguments.window_start_utc,
                    window_end_utc=arguments.window_end_utc,
                    rules=load_rules(arguments.rules),
                    max_commits=arguments.max_commits,
                    repository_filter=repository_filter,
                    max_compressed_bytes=arguments.max_compressed_bytes,
                    max_decompressed_bytes=arguments.max_decompressed_bytes,
                    max_records=arguments.max_records,
                    max_line_bytes=arguments.max_line_bytes,
                )
            )
        elif arguments.command == "report":
            _print(
                generate_report(
                    store, reports_dir=arguments.reports_dir, site_dir=arguments.site_dir
                )
            )
        elif arguments.command == "validate-report":
            _print(validate_pdf(arguments.pdf))
        elif arguments.command == "sample":
            _print(_sample(store, arguments))
        elif arguments.command == "loc":
            _print(
                measure_loc(
                    arguments.repository_dir, maximum_files=arguments.maximum_files
                )
            )
        else:
            parser.error(f"unsupported command: {arguments.command}")
    except (CollectionError, EventIngestError, LocMeasurementError, ReportError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
