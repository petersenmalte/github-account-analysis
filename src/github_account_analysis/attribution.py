"""Versioned, declaration-only commit attribution rules."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple


TRAILER_LINE = re.compile(r"^(?P<key>[A-Za-z][A-Za-z0-9-]*):[ \t]*(?P<value>.+)$")
DECLARED_AI = "ai-assisted"
DECLARED_HUMAN = "human-only"
INDETERMINATE = "indeterminate"


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
