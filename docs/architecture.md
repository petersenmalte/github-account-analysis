# Architecture decisions

## Bounded populations, not a census

The project has a repository-panel population and an observed-event population.
The panel is exactly the normalized repository URLs in
`config/repository-panel.json` (or a deterministic selection explicitly emitted
by a collector). The event population is exactly accepted records from source
files and their UTC observation windows. Reports state both frames, source
coverage, and gaps. This prevents a GitHub Search API 1,000-result cap, API
rate limits, and incomplete `PushEvent` payloads from being misrepresented as a
global repository or commit census.

## Strict declared attribution

`config/attribution-rules.v1.json` is a versioned contract. A commit is
`ai-assisted` or `human-only` only when a recognized trailer or a configured
exact co-author declaration is present. Commit-message mentions, generated-code
appearance, missing markers, and arbitrary co-authors are insufficient.
Conflicting, malformed, unsupported, or absent evidence is `indeterminate`.
Known bot identities are modelled separately and excluded from the human/AI
primary denominator. Tool names form a secondary multi-label breakdown; they
never split the primary AI count.

## State, provenance, and replay

DuckDB stores successful source records, failed source records, panel
snapshots, deduplicated commit facts, and repository-commit observation rows.
Commit facts are globally unique by SHA. Observation rows are unique by
`(source, repository, SHA)`, so the same SHA in two forks is one observed
commit fact and two repository observations. Inputs receive a content-derived
idempotence key. A rerun with the same content neither duplicates facts nor
observations.

The same state is exported to tracked Parquet files and a machine-readable
status/manifest. The raw event archive is an ephemeral input, not a durable
claim of completeness. If a source is missing, corrupt, or malformed, its
failure is recorded in DuckDB/status output and no source is marked fresh.
Source provenance records are retained, while detailed event facts and
repository observations are pruned transactionally whenever their normalized
UTC commit time falls outside the closed report window from ten years ago
through the current generation time. This bounds persistent state without
pretending the retained window is complete history.

An event selection identity derives from its analysis-schema revision, rule
version, and normalized repository filter. Attribution facts are stored per
selection identity. A report includes only sources, observations, and
attributions under the active identity, so a later scope/rule change cannot
silently combine its population with earlier observations. A rule/scope
transition starts a new observed history until matching sources are collected.

## Rendering and publishing

The report is built in a staging directory from a validated state snapshot.
Only after PDF preflight passes does staging atomically replace `reports/latest`;
the prior valid artifact remains intact on any failure. The semantic HTML,
PDF, JSON manifest, and SVG chart are copied to `site/` for GitHub Pages.
Page 1 is restricted to the current-data overview; trend, methodology, source,
and limitation details start on later pages.

The scheduled workflow processes one completed UTC GH Archive hour and retains
only events from the latest configured repository panel to keep runtime and
tracked-state growth bounded. That is a panel-scoped, time-of-day-biased
observed stream, not daily global activity. It commits successful
state/report updates before deploying a validated new Pages artifact. Failed
collection skips publishing and leaves the last deployment in place.
