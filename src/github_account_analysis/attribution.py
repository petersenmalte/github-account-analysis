"""Attribution and classification rules shared by the collector and report."""

from __future__ import annotations

import re
from collections import Counter
from typing import Any, Dict, Iterable, List, Mapping, Tuple

AI_TRAILER = re.compile(r"^AI-Classification:\s*([ABC])\s*$", re.IGNORECASE | re.MULTILINE)
COAUTHOR_TRAILER = re.compile(r"^Co-authored-by:\s*(.+?)\s*<([^>]+)>\s*$", re.IGNORECASE | re.MULTILINE)
GENERATED_PARTS = {"vendor", "vendors", "node_modules", "third_party", "third-party", "dist", "build", "generated", "gen"}


def is_bot(actor: Mapping[str, Any] | None) -> bool:
    """Recognize GitHub's bot account type or conventional bot login suffix."""
    if not actor:
        return False
    return actor.get("type") == "Bot" or str(actor.get("login", "")).lower().endswith("[bot]")


def classify_ai(message: str, pr_body: str = "") -> str:
    """Return only explicit A/B declarations; everything else is undetermined C."""
    declarations = {
        match.group(1).upper()
        for text in (message or "", pr_body or "")
        for match in AI_TRAILER.finditer(text)
    }
    return declarations.pop() if len(declarations) == 1 else "C"


def coauthors(message: str) -> List[Dict[str, str]]:
    """Return declared coauthors without assigning shares or resolving identities."""
    return [{"name": name.strip(), "email": email.strip()} for name, email in COAUTHOR_TRAILER.findall(message or "")]


def file_kind(path: str) -> str:
    """Classify excluded generated/vendor paths conservatively from their path."""
    parts = {part.lower() for part in path.replace("\\", "/").split("/")}
    if parts & {"vendor", "vendors", "node_modules", "third_party", "third-party"}:
        return "vendor"
    if parts & {"dist", "build", "generated", "gen"} or path.lower().endswith((".min.js", ".min.css", ".map")):
        return "generated"
    return "source"


def unique_commits(commits: Iterable[Mapping[str, Any]]) -> Tuple[List[Mapping[str, Any]], List[Dict[str, str]]]:
    """Deduplicate identical Git object IDs, retaining the first observed evidence."""
    retained: List[Mapping[str, Any]] = []
    exclusions: List[Dict[str, str]] = []
    seen: Dict[str, Mapping[str, Any]] = {}
    for commit in commits:
        sha = str(commit.get("sha", ""))
        if sha and sha in seen:
            exclusions.append(
                {
                    "sha": sha,
                    "reason": "identical commit object observed in multiple repositories or event sources",
                    "retained_repository": str(seen[sha].get("repository", "")),
                    "excluded_repository": str(commit.get("repository", "")),
                }
            )
            continue
        if sha:
            seen[sha] = commit
        retained.append(commit)
    return retained, exclusions


def summarize_classifications(items: Iterable[Mapping[str, Any]]) -> Dict[str, Any]:
    """Report classification counts and honest denominators, including unknowns."""
    human = [item for item in items if not item.get("bot")]
    counts = Counter(str(item.get("ai_classification", "C")) for item in human)
    denominator = len(human)
    return {
        "denominator": denominator,
        "basis": "attributable non-bot commit and pull-request artifacts with explicit metadata",
        "classes": {
            key: {
                "count": counts.get(key, 0),
                "percent": round((counts.get(key, 0) * 100 / denominator), 1) if denominator else None,
                "meaning": meaning,
            }
            for key, meaning in (
                ("A", "explicitly AI-assisted"),
                ("B", "explicitly entirely human declared"),
                ("C", "undetermined; no qualifying explicit declaration"),
            )
        },
        "bots": sum(1 for item in items if item.get("bot")),
    }
