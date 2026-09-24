"""Versioned attribution rules and compatibility helpers."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple


TRAILER_LINE = re.compile(r"^(?P<key>[A-Za-z][A-Za-z0-9-]*):[ \t]*(?P<value>.+)$")
DECLARED_AI = "ai-assisted"
DECLARED_HUMAN = "human-only"
INDETERMINATE = "indeterminate"
AI_TRAILER = re.compile(r"^AI-Classification:\s*([ABC])\s*$", re.IGNORECASE | re.MULTILINE)
COAUTHOR_TRAILER = re.compile(
    r"^Co-authored-by:\s*(.+?)\s*<([^>]+)>\s*$", re.IGNORECASE | re.MULTILINE
)
GENERATED_PARTS = {
    "vendor",
    "vendors",
    "node_modules",
    "third_party",
    "third-party",
    "dist",
    "build",
    "generated",
    "gen",
}


@dataclass(frozen=True)
class AttributionRules:
    """Immutable rules loaded from a checked-in versioned JSON document."""

    version: str
    ai_trailers: Mapping[str, Tuple[str, ...]]
    human_trailers: Mapping[str, Tuple[str, ...]]
    tool_trailers: Mapping[str, Tuple[str, ...]]
    exact_ai_coauthors: Tuple[Tuple[str, str], ...]
    bot_login_suffix: str
    bot_email_suffix: str


@dataclass(frozen=True)
class AttributionResult:
    declared_attribution: str
    is_bot: bool
    evidence: str
    tool_labels: Tuple[str, ...]
    rule_version: str


def _as_trailer_map(value: Mapping[str, Sequence[str]]) -> Dict[str, Tuple[str, ...]]:
    return {key: tuple(values) for key, values in value.items()}


def load_rules(path: Path) -> AttributionRules:
    with path.open(encoding="utf-8") as handle:
        raw = json.load(handle)
    trailers = raw["trailers"]
    coauthors = tuple(
        (entry["name"], entry["email"]) for entry in raw.get("exact_ai_coauthors", [])
    )
    return AttributionRules(
        version=raw["rule_version"],
        ai_trailers=_as_trailer_map(trailers["ai_assisted"]),
        human_trailers=_as_trailer_map(trailers["human_only"]),
        tool_trailers=_as_trailer_map(trailers["tools"]),
        exact_ai_coauthors=coauthors,
        bot_login_suffix=raw["bot_login_suffix"],
        bot_email_suffix=raw["bot_email_suffix"],
    )


def parse_trailer_footer(message: str) -> List[Tuple[str, str]]:
    """Return only a syntactically valid terminal Git-style trailer block."""
    stripped = message.rstrip("\n")
    if not stripped:
        return []
    parts = stripped.rsplit("\n\n", 1)
    footer = parts[-1].splitlines()
    trailers: List[Tuple[str, str]] = []
    for line in footer:
        matched = TRAILER_LINE.fullmatch(line)
        if matched is None:
            return []
        trailers.append((matched.group("key"), matched.group("value").strip()))
    return trailers


def _contains_declaration(
    trailers: Iterable[Tuple[str, str]], accepted: Mapping[str, Tuple[str, ...]]
) -> bool:
    return any(value in accepted.get(key, ()) for key, value in trailers)


def _trailer_values(trailers: Iterable[Tuple[str, str]], key: str) -> List[str]:
    return [value for actual_key, value in trailers if actual_key == key]


def _is_bot_identity(author_name: str, author_email: str, author_login: str, rules: AttributionRules) -> bool:
    return (
        author_login.endswith(rules.bot_login_suffix)
        or author_name.endswith(rules.bot_login_suffix)
        or author_email.endswith(rules.bot_email_suffix)
    )


def classify_commit(
    message: str,
    *,
    author_name: str = "",
    author_email: str = "",
    author_login: str = "",
    rules: AttributionRules,
) -> AttributionResult:
    """Classify explicit declarations; this intentionally makes no origin claim."""
    trailers = parse_trailer_footer(message)
    if _is_bot_identity(author_name, author_email, author_login, rules):
        return AttributionResult(
            declared_attribution=INDETERMINATE,
            is_bot=True,
            evidence="bot-identity; declaration excluded from primary attribution",
            tool_labels=(),
            rule_version=rules.version,
        )

    declared_ai = _contains_declaration(trailers, rules.ai_trailers)
    declared_human = _contains_declaration(trailers, rules.human_trailers)
    coauthors = tuple(_trailer_values(trailers, "Co-authored-by"))
    exact_coauthor_values = tuple(f"{name} <{email}>" for name, email in rules.exact_ai_coauthors)
    if any(coauthor in exact_coauthor_values for coauthor in coauthors):
        declared_ai = True

    if declared_ai and declared_human:
        return AttributionResult(
            declared_attribution=INDETERMINATE,
            is_bot=False,
            evidence="conflicting-explicit-declarations",
            tool_labels=(),
            rule_version=rules.version,
        )
    if declared_human:
        return AttributionResult(
            declared_attribution=DECLARED_HUMAN,
            is_bot=False,
            evidence="valid-human-only-declaration",
            tool_labels=(),
            rule_version=rules.version,
        )
    if not declared_ai:
        return AttributionResult(
            declared_attribution=INDETERMINATE,
            is_bot=False,
            evidence="no-valid-explicit-declaration",
            tool_labels=(),
            rule_version=rules.version,
        )

    allowed_tools = {
        tool for values in rules.tool_trailers.values() for tool in values
    }
    tool_values: List[str] = []
    for tool_key in rules.tool_trailers:
        for raw_value in _trailer_values(trailers, tool_key):
            tool_values.extend(item.strip() for item in raw_value.split(",") if item.strip())
    tools = tuple(dict.fromkeys(tool for tool in tool_values if tool in allowed_tools))
    return AttributionResult(
        declared_attribution=DECLARED_AI,
        is_bot=False,
        evidence="valid-ai-assisted-declaration",
        tool_labels=tools,
        rule_version=rules.version,
    )


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


# Best-effort text matching for known AI-tool names/footers. This is
# deliberately kept separate from classify_ai's strict trailer-only
# classification above: a name mention is not verified code origin, can miss
# real AI use, and can mis-flag human text that happens to name a tool
# (e.g. "cursor" as a UI element, "Claude" as a person's name). Consumers
# must present this as a heuristic signal, never as fact.
HEURISTIC_AI_SIGNALS: Tuple[Tuple[str, re.Pattern[str]], ...] = (
    ("Claude", re.compile(r"\bclaude\b", re.IGNORECASE)),
    ("GitHub Copilot", re.compile(r"\bcopilot\b", re.IGNORECASE)),
    ("ChatGPT/GPT", re.compile(r"\bchatgpt\b|\bgpt-\d", re.IGNORECASE)),
    ("Codex", re.compile(r"\bcodex\b", re.IGNORECASE)),
    ("Cursor", re.compile(r"\bcursor(\.so|\.com)?\s*(ai|ide)\b|\bcursor ai\b", re.IGNORECASE)),
    ("Devin", re.compile(r"\bdevin\b", re.IGNORECASE)),
    ("Gemini", re.compile(r"\bgemini\b", re.IGNORECASE)),
    ("Amazon Q", re.compile(r"\bamazon q\b", re.IGNORECASE)),
    ("Codeium", re.compile(r"\bcodeium\b", re.IGNORECASE)),
    ("Tabnine", re.compile(r"\btabnine\b", re.IGNORECASE)),
    ("Sourcegraph Cody", re.compile(r"\bsourcegraph cody\b", re.IGNORECASE)),
    ("Generic AI-tool footer marker", re.compile(r"🤖")),
)


def heuristic_ai_mentions(message: str, pr_body: str = "") -> List[str]:
    """Return known AI-tool names/markers found by loose text matching.

    Unlike classify_ai, this has no trailer-syntax requirement and no
    A/B/C exclusivity rule — it is a lightweight, uncertain signal, not a
    classification. Order follows HEURISTIC_AI_SIGNALS; duplicates removed.
    """
    text = f"{message or ''}\n{pr_body or ''}"
    return [label for label, pattern in HEURISTIC_AI_SIGNALS if pattern.search(text)]


def coauthors(message: str) -> List[Dict[str, str]]:
    """Return declared coauthors without assigning shares or resolving identities."""
    return [
        {"name": name.strip(), "email": email.strip()}
        for name, email in COAUTHOR_TRAILER.findall(message or "")
    ]


def file_kind(path: str) -> str:
    """Classify excluded generated/vendor paths conservatively from their path."""
    parts = {part.lower() for part in path.replace("\\", "/").split("/")}
    if parts & {"vendor", "vendors", "node_modules", "third_party", "third-party"}:
        return "vendor"
    if parts & {"dist", "build", "generated", "gen"} or path.lower().endswith(
        (".min.js", ".min.css", ".map")
    ):
        return "generated"
    return "source"


def unique_commits(
    commits: Iterable[Mapping[str, Any]],
) -> Tuple[List[Mapping[str, Any]], List[Dict[str, str]]]:
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
                "percent": round((counts.get(key, 0) * 100 / denominator), 1)
                if denominator
                else None,
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
