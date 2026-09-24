"""Evidence-bound metric construction and a minimal selectable-text PDF writer."""

from __future__ import annotations

import json
import platform
import textwrap
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping

from . import __version__
from .attribution import classify_ai, coauthors, file_kind, is_bot, summarize_classifications, unique_commits


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
        "changes": {"additions": additions, "deletions": deletions, "excluded_additions": excluded_additions, "excluded_deletions": excluded_deletions},
        "excluded_files": excluded_files,
        "source_url": item.get("html_url") or (f"https://github.com/{item.get('repository')}/commit/{item.get('sha')}" if item.get("repository") else ""),
        "uncertainty": (["committer differs from author; this report does not equate the roles"] if (item.get("committer") or {}).get("login") and (item.get("committer") or {}).get("login") != actor.get("login") else []),
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
        "uncertainty": ["a squash merge may not preserve pull-request authorship in commit history"] if item.get("merged_at") else [],
    }


def build_report(request: Mapping[str, Any], data: Mapping[str, Any]) -> Dict[str, Any]:
    """Build transparent metrics from only explicitly provided public API evidence."""
    profile = data.get("profile") or {}
    login = str(profile.get("login") or request.get("username") or "")
    commits, deduplicated = unique_commits(data.get("commits") or [])
    commit_records = [record for item in commits if (record := _commit_record(item, login))]
    raw_pull_records = [record for item in data.get("pulls") or [] if (record := _pull_record(item, login))]
    pull_records, pull_exclusions = [], []
    pull_keys = set()
    for record in raw_pull_records:
        key = (record["repository"], record["number"])
        if key in pull_keys:
            pull_exclusions.append({"artifact": f"PR {record['repository']}#{record['number']}", "reason": "duplicate pull-request evidence"})
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
    contributed = {str(item.get("repository")) for item in artifacts if item.get("ownership") == "contributed" and item.get("repository")}
    uncertainty = [
        "Attribution is account-first: commits without this GitHub author account are excluded, even if an email/name looks similar.",
        "Co-authored-by trailers establish shared coauthorship only; no individual change shares are inferred.",
        "Transferred repositories, rewritten history, unavailable API pages, squash merges, and private or missing activity can make this incomplete.",
        "Repository ownership and contribution are separate; other contributors' work is not credited to the analyzed account.",
    ]
    report = {
        "schema_version": "1.0",
        "report_id": str(uuid.uuid4()),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "subject": {"login": login, "profile_url": profile.get("html_url", f"https://github.com/{login}"), "public_profile_name": profile.get("name")},
        "config": {
            "timeframe": request.get("timeframe", "all_available"),
            "scope": sorted(request.get("scope") or []),
            "public_data_only": True,
            "credentials_used": False,
            "target_code_executed": False,
            "api_request_budget": request.get("api_request_budget", 40),
        },
        "tool_versions": {"github_account_analysis": __version__, "python": platform.python_version(), "quality_tools": {"CodeQL": "not installed/not executed", "Semgrep": "not installed/not executed", "SonarQube": "not configured/not executed"}},
        "metrics": {
            "attributable_commits": len(commit_records),
            "opened_pull_requests": len(pull_records),
            "merged_pull_requests": sum(1 for item in pull_records if item["merged"]),
            "owned_repositories": len(owned),
            "contributed_repositories": len(contributed),
            "code_change_volume": {"added_lines": additions, "removed_lines": deletions, "excluded_generated_vendor_added_lines": excluded_additions, "excluded_generated_vendor_removed_lines": excluded_deletions, "meaning": "diff line volume, not productivity, effort, or unique authored code"},
            "languages_in_owned_repositories": {"basis": "owned repositories whose GitHub repository summary reports this primary language", "repository_counts": dict(languages.most_common())},
            "monthly_activity": dict(sorted(activity.items())),
            "yearly_activity": dict(sorted(Counter(key[:4] for key, count in activity.items() for _ in range(count)).items())),
        },
        "ai_metadata": summarize_classifications(artifacts),
        "quality": {
            "repository_state": {"status": "not measured", "reason": "Target repositories are untrusted and are not cloned, built, or tested by this public-profile workflow."},
            "attributable_change_evidence": {"status": "not measured", "reason": "No code-level provenance or safe static-analysis snapshot was available."},
            "dimensions": {name: "not measured" for name in ("security", "maintainability_complexity", "duplication", "rule_violations", "coverage")},
            "historical_view": {"status": "not measured", "reason": "Representative historical states require a separately configured, version-pinned static-analysis job; no every-commit scan is implied."},
        },
        "artifacts": artifacts,
        "deduplication_exclusions": deduplicated + pull_exclusions,
        "uncertainty": uncertainty,
        "partial": bool(data.get("partial_reasons")),
        "partial_reasons": list(dict.fromkeys(data.get("partial_reasons") or [])),
        "provenance": data.get("provenance") or [],
    }
    return report


def report_lines(report: Mapping[str, Any]) -> List[str]:
    """Create human-readable, neutral report text for a selectable-text PDF."""
    metrics = report["metrics"]
    ai = report["ai_metadata"]
    lines = [
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
        *([f"Partial analysis: {reason}" for reason in report["partial_reasons"]] or ["No collector partiality was reported."]),
        *([f"Excluded duplicate {item.get('sha') or item.get('artifact')}: {item['reason']}" for item in report["deduplication_exclusions"]] or ["No duplicate commit or pull-request exclusions recorded."]),
        "",
        "Source links",
        *[str(entry.get("url")) for entry in report["provenance"]],
    ]
    return lines


def _pdf_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def render_pdf(report: Mapping[str, Any]) -> bytes:
    """Write a standards-compliant PDF using text operators, preserving selection."""
    raw_lines = report_lines(report)
    lines = [segment for line in raw_lines for segment in (textwrap.wrap(line, width=102, break_long_words=False) or [""])]
    per_page = 52
    pages = [lines[index : index + per_page] for index in range(0, len(lines), per_page)] or [["No report data."]]
    objects: List[bytes] = [b"<< /Type /Catalog /Pages 2 0 R >>", b"", b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
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
        objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> /Contents {content_id} 0 R >>".encode())
        objects.append(f"<< /Length {len(content_bytes)} >>\nstream\n".encode() + content_bytes + b"\nendstream")
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
    output.extend(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return bytes(output)
