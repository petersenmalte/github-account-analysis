"""Semantic HTML, selectable-text PDF, and static-site report generation."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import tempfile
from typing import Any, Dict, Iterable, Mapping, Optional

from jinja2 import Environment, FileSystemLoader, select_autoescape
from matplotlib import font_manager
import matplotlib.pyplot as plt
from pypdf import PdfReader

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
    pdf_bytes = path.read_bytes()
    if b"/FontFile" not in pdf_bytes:
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
        "embedded_font_stream": True,
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
