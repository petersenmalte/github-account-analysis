"""Semantic HTML, selectable-text PDF, and static-site report generation."""

from __future__ import annotations

import hashlib
import json
import platform
from pathlib import Path
import shutil
import tempfile
import textwrap
import uuid
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional

from jinja2 import Environment, FileSystemLoader, select_autoescape
from matplotlib import font_manager
import matplotlib.pyplot as plt
from pypdf import PdfReader

from . import __version__
from .attribution import (
    classify_ai,
    coauthors,
    file_kind,
    is_bot,
    summarize_classifications,
    unique_commits,
)
from .metrics import build_report_data
from .storage import AnalyticsStore
from .utils import atomic_write_json, format_utc, parse_utc, utc_now_iso


class ReportError(RuntimeError):
    """A reporting failure that must retain the last valid report."""


class ReportValidationError(ReportError):
    """A report that cannot be safely published."""


PACKAGE_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = PACKAGE_DIR / "templates"


def _write_chart(path: Path, monthly: Iterable[Mapping[str, Any]]) -> None:
    rows = list(monthly)
    figure, axis = plt.subplots(figsize=(8.5, 3.1))
    if rows:
        labels = [row["month_utc"] for row in rows]
        axis.plot(labels, [row["declared_ai_assisted"] for row in rows], label="Declared AI-assisted")
        axis.plot(labels, [row["declared_human_only"] for row in rows], label="Declared human-only")
        axis.plot(labels, [row["indeterminate"] for row in rows], label="Indeterminate")
        axis.tick_params(axis="x", rotation=45, labelsize=8)
        axis.set_ylabel("Unique observed commit facts")
        axis.legend(fontsize=8)
        axis.grid(axis="y", alpha=0.25)
    else:
        axis.axis("off")
        axis.text(
            0.5,
            0.5,
            "No observed-event months are available. No trend is inferred.",
            ha="center",
            va="center",
            transform=axis.transAxes,
        )
    figure.tight_layout()
    figure.savefig(path, format="svg")
    plt.close(figure)


def _write_css(path: Path) -> None:
    path.write_text(
        """
@font-face {
  font-family: "Analysis Sans";
  src: url("assets/DejaVuSans.ttf") format("truetype");
}
@page {
  size: A4;
  margin: 15mm;
  @bottom-center {
    content: "github-account-analysis · " counter(page);
    font-family: "Analysis Sans";
    font-size: 8pt;
  }
}
* { box-sizing: border-box; }
body { color: #172033; font-family: "Analysis Sans", sans-serif; font-size: 10pt; line-height: 1.42; }
h1 { color: #102a43; font-size: 25pt; line-height: 1.15; margin: 0 0 6pt; }
h2 { color: #102a43; font-size: 16pt; margin: 0 0 8pt; }
h3 { color: #243b53; font-size: 11pt; margin: 10pt 0 4pt; }
p, li { margin-top: 0; }
a { color: #005cc5; overflow-wrap: anywhere; }
.subtitle { color: #486581; margin: 0 0 12pt; }
.notice { background: #fff3cd; border: 1px solid #d39e00; padding: 7pt; }
.metric-grid { display: flex; flex-wrap: wrap; gap: 7pt; margin: 8pt 0; }
.metric { background: #f0f4f8; border-left: 3pt solid #268bd2; min-width: 30%; padding: 7pt; }
.metric strong { display: block; font-size: 16pt; color: #102a43; }
table { border-collapse: collapse; font-size: 8.6pt; margin: 7pt 0 12pt; width: 100%; }
th, td { border: 1px solid #bcccdc; padding: 4pt; text-align: left; vertical-align: top; }
th { background: #d9e2ec; }
.overview { break-after: page; page-break-after: always; }
.overview .statement { font-size: 11pt; }
.muted { color: #627d98; }
.source-list { font-size: 8pt; }
.chart { max-height: 260pt; max-width: 100%; width: 100%; }
""".strip()
        + "\n",
        encoding="utf-8",
    )


def _json_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_pdf(path: Path) -> Dict[str, Any]:
    """Check publication-critical PDF properties without rasterizing it."""
    if not path.is_file():
        raise ReportValidationError(f"missing PDF: {path}")
    try:
        reader = PdfReader(str(path))
    except Exception as error:
        raise ReportValidationError(f"PDF cannot be read: {error}") from error
    if len(reader.pages) < 2:
        raise ReportValidationError("PDF must have a distinct overview page and later detail pages")
    page_one_text = reader.pages[0].extract_text() or ""
    if "Current data overview" not in page_one_text:
        raise ReportValidationError("PDF page 1 lacks the current-data overview")
    disallowed_page_one = ("Trend from observed event data", "Methodology and limits", "Sources and coverage")
    if any(heading in page_one_text for heading in disallowed_page_one):
        raise ReportValidationError("PDF page 1 contains later-page detail")
    if len(page_one_text.strip()) < 80:
        raise ReportValidationError("PDF page 1 text is not extractable enough to be selectable")
    embedded_font_stream = False
    for page in reader.pages:
        fonts = (page.get("/Resources") or {}).get("/Font", {})
        for font_ref in fonts.values():
            font = font_ref.get_object()
            descriptors = []
            direct_descriptor = font.get("/FontDescriptor")
            if direct_descriptor:
                descriptors.append(direct_descriptor.get_object())
            for descendant_ref in font.get("/DescendantFonts", []):
                descendant = descendant_ref.get_object()
                descendant_descriptor = descendant.get("/FontDescriptor")
                if descendant_descriptor:
                    descriptors.append(descendant_descriptor.get_object())
            if any(
                key in descriptor
                for descriptor in descriptors
                for key in ("/FontFile", "/FontFile2", "/FontFile3")
            ):
                embedded_font_stream = True
                break
        if embedded_font_stream:
            break
    if not embedded_font_stream:
        raise ReportValidationError("PDF has no embedded font stream")
    uri_count = 0
    for page in reader.pages:
        annotations = page.get("/Annots", [])
        for annotation_ref in annotations:
            annotation = annotation_ref.get_object()
            action = annotation.get("/A")
            if action and action.get("/URI"):
                uri_count += 1
    if uri_count == 0:
        raise ReportValidationError("PDF has no working URI link annotations")
    return {
        "page_count": len(reader.pages),
        "page_one_extractable_characters": len(page_one_text.strip()),
        "uri_link_count": uri_count,
        "embedded_font_stream": embedded_font_stream,
    }


def _render_stage(data: Mapping[str, Any], stage: Path) -> Dict[str, Any]:
    assets = stage / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    font_path = Path(font_manager.findfont("DejaVu Sans", fallback_to_default=True))
    if not font_path.is_file():
        raise ReportError("a usable TrueType font was not found for PDF embedding")
    shutil.copy2(font_path, assets / "DejaVuSans.ttf")
    _write_css(stage / "report.css")
    _write_chart(stage / "trend.svg", data["monthly_trend_last_ten_years"])
    template = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(["html", "xml"]),
    ).get_template("report.html.j2")
    synthetic = any(
        str(source["source_url"]).startswith("synthetic://") for source in data["sources"]
    )
    html = template.render(
        report=data,
        synthetic=synthetic,
        official_sources=[
            ("GitHub REST API documentation", "https://docs.github.com/rest"),
            ("GitHub Search API limitations", "https://docs.github.com/rest/search/search"),
            ("GH Archive", "https://www.gharchive.org/"),
        ],
    )
    (stage / "report.html").write_text(html, encoding="utf-8")
    manifest = dict(data)
    manifest["schema_version"] = "report-manifest/v1"
    manifest["artifacts"] = {
        "html": "report.html",
        "pdf": "report.pdf",
        "trend_chart": "trend.svg",
    }
    atomic_write_json(stage / "report-manifest.json", manifest)
    try:
        from weasyprint import HTML
    except (ImportError, OSError) as error:
        raise ReportError(
            "WeasyPrint native libraries are unavailable; PDF generation was not attempted"
        ) from error
    try:
        HTML(string=html, base_url=str(stage)).write_pdf(str(stage / "report.pdf"))
    except (OSError, ValueError) as error:
        raise ReportError(f"WeasyPrint failed to generate the PDF: {error}") from error
    validation = validate_pdf(stage / "report.pdf")
    manifest["pdf_validation"] = validation
    manifest["artifacts"]["report_manifest_sha256"] = _json_digest(stage / "report-manifest.json")
    atomic_write_json(stage / "report-manifest.json", manifest)
    return manifest


def _replace_latest(staging: Path, reports_dir: Path) -> None:
    latest = reports_dir / "latest"
    history = reports_dir / "history"
    history.mkdir(parents=True, exist_ok=True)
    if latest.exists():
        stamp = utc_now_iso().replace(":", "-")
        latest.replace(history / f"valid-{stamp}")
    staging.replace(latest)


def _publish_site(latest: Path, site_dir: Path) -> None:
    stage = Path(tempfile.mkdtemp(prefix=".staging-", dir=site_dir))
    try:
        for filename in ("report.html", "report.css", "report.pdf", "report-manifest.json", "trend.svg"):
            shutil.copy2(latest / filename, stage / filename)
        shutil.copytree(latest / "assets", stage / "assets")
        index = (stage / "report.html").read_text(encoding="utf-8").replace(
            "<title>Declared-attribution public GitHub analysis</title>",
            "<title>Public GitHub analysis</title>",
        )
        (stage / "index.html").write_text(index, encoding="utf-8")
        for source in stage.iterdir():
            if source.name == "assets":
                target = site_dir / "assets"
                if target.exists():
                    shutil.rmtree(target)
                source.replace(target)
            else:
                source.replace(site_dir / source.name)
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def write_report_failure_status(
    reports_dir: Path, error: Exception, attempted_data: Optional[Mapping[str, Any]] = None
) -> None:
    latest = reports_dir / "latest"
    failed_manifest = None
    if attempted_data is not None:
        failed_manifest = reports_dir / "last-failed-render-attempt.json"
        payload = dict(attempted_data)
        payload["schema_version"] = "failed-render-attempt/v1"
        payload["render_status"] = "failed"
        payload["render_error"] = str(error)
        payload["staleness_note"] = (
            "This is a machine-readable data snapshot from a failed render attempt, "
            "not a valid published report."
        )
        atomic_write_json(failed_manifest, payload)
    atomic_write_json(
        reports_dir / "status.json",
        {
            "schema_version": "report-status/v1",
            "status": "failed",
            "attempted_at_utc": utc_now_iso(),
            "last_valid_report": str(latest / "report.pdf") if (latest / "report.pdf").is_file() else None,
            "last_failed_render_attempt": str(failed_manifest) if failed_manifest else None,
            "staleness_note": "The previous valid report was retained; this attempted report was not published.",
            "error": str(error),
        },
    )


def generate_report(store: AnalyticsStore, *, reports_dir: Path, site_dir: Path) -> Dict[str, Any]:
    """Create and validate a report before replacing any published artifact."""
    reports_dir.mkdir(parents=True, exist_ok=True)
    site_dir.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=reports_dir))
    data: Optional[Mapping[str, Any]] = None
    try:
        data = build_report_data(store)
        manifest = _render_stage(data, staging)
        _replace_latest(staging, reports_dir)
        _publish_site(reports_dir / "latest", site_dir)
        atomic_write_json(
            reports_dir / "status.json",
            {
                "schema_version": "report-status/v1",
                "status": "valid",
                "generated_at_utc": manifest["generated_at_utc"],
                "last_valid_report": str(reports_dir / "latest" / "report.pdf"),
                "staleness_note": "Current report was generated from the recorded source snapshot.",
            },
        )
        return manifest
    except Exception as error:
        write_report_failure_status(reports_dir, error, attempted_data=data)
        if isinstance(error, ReportError):
            raise
        raise ReportError(str(error)) from error
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _actor(item: Mapping[str, Any]) -> Mapping[str, Any]:
    return item.get("author") or item.get("user") or {}


def _commit_date(item: Mapping[str, Any]) -> str:
    return str(((item.get("commit") or {}).get("author") or {}).get("date") or "")


def _activity_key(timestamp: str) -> str:
    try:
        return datetime.fromisoformat(timestamp.replace("Z", "+00:00")).strftime("%Y-%m")
    except ValueError:
        return "unknown"


def _commit_record(item: Mapping[str, Any], login: str) -> Dict[str, Any] | None:
    actor = _actor(item)
    if str(actor.get("login", "")).lower() != login.lower():
        return None
    message = str((item.get("commit") or {}).get("message", ""))
    files = item.get("files") or []
    source_files, excluded_files = [], []
    additions = deletions = excluded_additions = excluded_deletions = 0
    for file in files:
        kind = file_kind(str(file.get("filename", "")))
        destination = excluded_files if kind != "source" else source_files
        destination.append({"path": file.get("filename", ""), "kind": kind})
        if kind == "source":
            additions += int(file.get("additions", 0) or 0)
            deletions += int(file.get("deletions", 0) or 0)
        else:
            excluded_additions += int(file.get("additions", 0) or 0)
            excluded_deletions += int(file.get("deletions", 0) or 0)
    return {
        "type": "commit",
        "sha": item.get("sha", ""),
        "repository": item.get("repository", ""),
        "ownership": item.get("ownership", "unknown"),
        "date": _commit_date(item),
        "author_account": actor.get("login"),
        "committer_account": (item.get("committer") or {}).get("login"),
        "bot": is_bot(actor),
        "ai_classification": classify_ai(message),
        "coauthors": coauthors(message),
        "changes": {
            "additions": additions,
            "deletions": deletions,
            "excluded_additions": excluded_additions,
            "excluded_deletions": excluded_deletions,
        },
        "excluded_files": excluded_files,
        "source_url": item.get("html_url")
        or (
            f"https://github.com/{item.get('repository')}/commit/{item.get('sha')}"
            if item.get("repository")
            else ""
        ),
        "uncertainty": (
            ["committer differs from author; this report does not equate the roles"]
            if (item.get("committer") or {}).get("login")
            and (item.get("committer") or {}).get("login") != actor.get("login")
            else []
        ),
    }


def _pull_record(item: Mapping[str, Any], login: str) -> Dict[str, Any] | None:
    actor = _actor(item)
    if str(actor.get("login", "")).lower() != login.lower():
        return None
    body = str(item.get("body") or "")
    return {
        "type": "pull_request",
        "number": item.get("number"),
        "repository": item.get("repository", ""),
        "ownership": item.get("ownership", "contributed"),
        "date": item.get("created_at", ""),
        "merged": bool(item.get("merged_at")),
        "author_account": actor.get("login"),
        "bot": is_bot(actor),
        "ai_classification": classify_ai("", body),
        "source_url": item.get("html_url", ""),
        "uncertainty": (
            ["a squash merge may not preserve pull-request authorship in commit history"]
            if item.get("merged_at")
            else []
        ),
    }


def build_report(request: Mapping[str, Any], data: Mapping[str, Any]) -> Dict[str, Any]:
    """Build transparent metrics from only explicitly provided public API evidence."""
    profile = data.get("profile") or {}
    login = str(profile.get("login") or request.get("username") or "")
    commits, deduplicated = unique_commits(data.get("commits") or [])
    commit_records = [record for item in commits if (record := _commit_record(item, login))]
    raw_pull_records = [
        record for item in data.get("pulls") or [] if (record := _pull_record(item, login))
    ]
    pull_records = []
    pull_exclusions = []
    pull_keys = set()
    for record in raw_pull_records:
        key = (record["repository"], record["number"])
        if key in pull_keys:
            pull_exclusions.append(
                {
                    "artifact": f"PR {record['repository']}#{record['number']}",
                    "reason": "duplicate pull-request evidence",
                }
            )
            continue
        pull_keys.add(key)
        pull_records.append(record)
    artifacts = commit_records + pull_records
    activity = Counter(_activity_key(str(item.get("date", ""))) for item in artifacts)
    languages = Counter()
    for repo in data.get("repos") or []:
        if repo.get("language"):
            languages[str(repo["language"])] += 1
    additions = sum(item["changes"]["additions"] for item in commit_records)
    deletions = sum(item["changes"]["deletions"] for item in commit_records)
    excluded_additions = sum(item["changes"]["excluded_additions"] for item in commit_records)
    excluded_deletions = sum(item["changes"]["excluded_deletions"] for item in commit_records)
    owned = {str(repo.get("full_name")) for repo in data.get("repos") or [] if repo.get("full_name")}
    contributed = {
        str(item.get("repository"))
        for item in artifacts
        if item.get("ownership") == "contributed" and item.get("repository")
    }
    uncertainty = [
        "Attribution is account-first: commits without this GitHub author account are excluded, even if an email/name looks similar.",
        "Co-authored-by trailers establish shared coauthorship only; no individual change shares are inferred.",
        "Transferred repositories, rewritten history, unavailable API pages, squash merges, and private or missing activity can make this incomplete.",
        "Repository ownership and contribution are separate; other contributors' work is not credited to the analyzed account.",
    ]
    return {
        "schema_version": "1.0",
        "report_id": str(uuid.uuid4()),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "subject": {
            "login": login,
            "profile_url": profile.get("html_url", f"https://github.com/{login}"),
            "public_profile_name": profile.get("name"),
        },
        "config": {
            "timeframe": request.get("timeframe", "all_available"),
            "scope": sorted(request.get("scope") or []),
            "public_data_only": True,
            "credentials_used": False,
            "target_code_executed": False,
            "api_request_budget": request.get("api_request_budget", 40),
        },
        "tool_versions": {
            "github_account_analysis": __version__,
            "python": platform.python_version(),
            "quality_tools": {
                "CodeQL": "not installed/not executed",
                "Semgrep": "not installed/not executed",
                "SonarQube": "not configured/not executed",
            },
        },
        "metrics": {
            "attributable_commits": len(commit_records),
            "opened_pull_requests": len(pull_records),
            "merged_pull_requests": sum(1 for item in pull_records if item["merged"]),
            "owned_repositories": len(owned),
            "contributed_repositories": len(contributed),
            "code_change_volume": {
                "added_lines": additions,
                "removed_lines": deletions,
                "excluded_generated_vendor_added_lines": excluded_additions,
                "excluded_generated_vendor_removed_lines": excluded_deletions,
                "meaning": "diff line volume, not productivity, effort, or unique authored code",
            },
            "languages_in_owned_repositories": {
                "basis": "owned repositories whose GitHub repository summary reports this primary language",
                "repository_counts": dict(languages.most_common()),
            },
            "monthly_activity": dict(sorted(activity.items())),
            "yearly_activity": dict(
                sorted(Counter(key[:4] for key, count in activity.items() for _ in range(count)).items())
            ),
        },
        "ai_metadata": summarize_classifications(artifacts),
        "quality": {
            "repository_state": {
                "status": "not measured",
                "reason": "Target repositories are untrusted and are not cloned, built, or tested by this public-profile workflow.",
            },
            "attributable_change_evidence": {
                "status": "not measured",
                "reason": "No code-level provenance or safe static-analysis snapshot was available.",
            },
            "dimensions": {
                name: "not measured"
                for name in (
                    "security",
                    "maintainability_complexity",
                    "duplication",
                    "rule_violations",
                    "coverage",
                )
            },
            "historical_view": {
                "status": "not measured",
                "reason": "Representative historical states require a separately configured, version-pinned static-analysis job; no every-commit scan is implied.",
            },
        },
        "artifacts": artifacts,
        "deduplication_exclusions": deduplicated + pull_exclusions,
        "uncertainty": uncertainty,
        "partial": bool(data.get("partial_reasons")),
        "partial_reasons": list(dict.fromkeys(data.get("partial_reasons") or [])),
        "provenance": data.get("provenance") or [],
    }


def report_lines(report: Mapping[str, Any]) -> List[str]:
    """Create human-readable, neutral report text for a selectable-text PDF."""
    metrics = report["metrics"]
    ai = report["ai_metadata"]
    return [
        "Public GitHub Account Analysis",
        f"Subject: @{report['subject']['login']}  Profile: {report['subject']['profile_url']}",
        f"Generated: {report['generated_at']}  Report ID: {report['report_id']}",
        "",
        "Scope and evidence",
        f"Timeframe: {report['config']['timeframe']}; scope: {', '.join(report['config']['scope']) or 'none'}",
        "Only public GitHub API information was analyzed. No credentials, private activity, external identity research, target builds, or target tests were used.",
        "",
        "Attributable public technical activity",
        f"Commits: {metrics['attributable_commits']}; opened PRs: {metrics['opened_pull_requests']}; merged PRs: {metrics['merged_pull_requests']}",
        f"Owned repositories: {metrics['owned_repositories']}; contributed repositories: {metrics['contributed_repositories']}",
        f"Code change volume: +{metrics['code_change_volume']['added_lines']} / -{metrics['code_change_volume']['removed_lines']} lines (not productivity or unique authored code).",
        f"Generated/vendor excluded from source volume: +{metrics['code_change_volume']['excluded_generated_vendor_added_lines']} / -{metrics['code_change_volume']['excluded_generated_vendor_removed_lines']}.",
        f"Languages in owned repositories (primary-language repository counts): {json.dumps(metrics['languages_in_owned_repositories']['repository_counts'])}",
        f"Monthly activity: {json.dumps(metrics['monthly_activity'])}",
        "",
        "Explicit AI metadata (artifact denominator excludes bots)",
        f"Denominator: {ai['denominator']}. A explicitly AI-assisted: {ai['classes']['A']['count']} ({ai['classes']['A']['percent']}%).",
        f"B explicitly entirely human declared: {ai['classes']['B']['count']} ({ai['classes']['B']['percent']}%).",
        f"C undetermined: {ai['classes']['C']['count']} ({ai['classes']['C']['percent']}%). Bots: {ai['bots']}.",
        "Only an explicit AI-Classification trailer or attributable PR metadata qualifies. No AI-written-code percentage is reported.",
        "",
        "Quality measurement",
        "Security, maintainability/complexity, duplication, rule violations, and coverage are each not measured unless a separate safe, version-pinned static-analysis run is configured.",
        "",
        "Uncertainty and exclusions",
        *report["uncertainty"],
        *(
            [f"Partial analysis: {reason}" for reason in report["partial_reasons"]]
            or ["No collector partiality was reported."]
        ),
        *(
            [
                f"Excluded duplicate {item.get('sha') or item.get('artifact')}: {item['reason']}"
                for item in report["deduplication_exclusions"]
            ]
            or ["No duplicate commit or pull-request exclusions recorded."]
        ),
        "",
        "Source links",
        *[str(entry.get("url")) for entry in report["provenance"]],
    ]


def _pdf_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def render_pdf(report: Mapping[str, Any]) -> bytes:
    """Write a standards-compliant PDF using text operators, preserving selection."""
    raw_lines = report_lines(report)
    lines = [
        segment
        for line in raw_lines
        for segment in (textwrap.wrap(line, width=102, break_long_words=False) or [""])
    ]
    per_page = 52
    pages = [lines[index : index + per_page] for index in range(0, len(lines), per_page)] or [
        ["No report data."]
    ]
    objects: List[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    page_refs = []
    for page_lines in pages:
        content = ["BT", "/F1 9 Tf", "50 750 Td", "12 TL"]
        for line in page_lines:
            content.append(f"({_pdf_escape(line)}) Tj")
            content.append("T*")
        content.append("ET")
        content_bytes = "\n".join(content).encode("latin-1", "replace")
        content_id = len(objects) + 2
        page_id = len(objects) + 1
        page_refs.append(f"{page_id} 0 R")
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> /Contents {content_id} 0 R >>".encode()
        )
        objects.append(
            f"<< /Length {len(content_bytes)} >>\nstream\n".encode()
            + content_bytes
            + b"\nendstream"
        )
    objects[1] = f"<< /Type /Pages /Kids [{' '.join(page_refs)}] /Count {len(pages)} >>".encode()
    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, object_value in enumerate(objects, start=1):
        offsets.append(len(output))
        output.extend(f"{number} 0 obj\n".encode())
        output.extend(object_value)
        output.extend(b"\nendobj\n")
    xref = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    output.extend(b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets[1:]))
    output.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(output)
