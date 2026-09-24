# Public GitHub account analysis

This is a reproducible analysis tool for **bounded public GitHub data**. It
does not attempt to classify code as AI- or human-written. Its primary trend is
named **declared-attribution**: an explicit, versioned declaration can show
that a contributor declared AI assistance or exclusively human work under these
rules; it is **not proof of origin**.

The project intentionally reports two distinct populations:

1. A deterministic repository panel configured in
   [`config/repository-panel.json`](config/repository-panel.json), for public
   repository metadata and language analysis.
2. An observed public-event stream from GH Archive-like files, for activity and
   declared-attribution trends.

Neither is an all-GitHub repository, commit, or activity census. Do not call a
result representative unless independent sampling evidence substantiates that
claim.

## Quick start

Requires Python 3.9 or newer. The report stack uses system libraries required
by [WeasyPrint](https://doc.courtbouillon.org/weasyprint/stable/first_steps.html);
GitHub Actions uses Ubuntu where these are available.

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[test]'

# Explicitly synthetic: creates data, report, and Pages-compatible site elsewhere.
github-account-analysis --data-dir /tmp/gaa-sample sample \
  --reports-dir /tmp/gaa-reports --site-dir /tmp/gaa-site

# Run all checks.
pytest
```

The bundled `examples/synthetic/` fixture is only a demonstrator. Its report
has a synthetic banner and must never be described as measured GitHub data.

## Single-profile browser analyzer

For an interactive, one-account report (rather than the fixed repository
panel below), run the local web UI:

```bash
python -m pip install -e '.[test]'
PYTHONPATH=src python3 -m github_account_analysis.app
# Open http://127.0.0.1:8000
```

Enter a GitHub username or profile URL, choose a timeframe and scope, and
click **Analyze public profile**. By default this only makes anonymous
GitHub REST API requests, which GitHub caps at 60 requests/hour/IP; an
account with several owned repositories can exhaust that within one run and
the report comes back `"partial": true` with `"... request skipped"` reasons
listed under `partial_reasons`. Set `GITHUB_TOKEN` to a
[personal access token](https://docs.github.com/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens)
(no scopes needed for public data) before starting the server to raise the
limit to 5000 requests/hour:

```bash
export GITHUB_TOKEN=github_pat_...  # never printed or included in the report
PYTHONPATH=src python3 -m github_account_analysis.app
```

The report's `config.credentials_used` field always reflects whether a token
was used for that run.

## Real public-data run

The supplied panel is a small, fixed list of public repositories, not a
representative sample. Configure repository URLs or a deterministic observed
repository selection in `config/repository-panel.json`, then collect:

```bash
# Optional: a token increases GitHub REST API allowance. Never print it.
export GITHUB_TOKEN=github_pat_...
github-account-analysis collect-panel --config config/repository-panel.json

# The source window is mandatory and explicitly UTC.
curl --fail --location \
  https://data.gharchive.org/2025-02-01-0.json.gz \
  -o /tmp/2025-02-01-0.json.gz
github-account-analysis ingest-events \
  --input /tmp/2025-02-01-0.json.gz \
  --source-url https://data.gharchive.org/2025-02-01-0.json.gz \
  --window-start-utc 2025-02-01T00:00:00Z \
  --window-end-utc 2025-02-01T01:00:00Z \
  --panel-repositories-only
github-account-analysis report
github-account-analysis validate-report --pdf reports/latest/report.pdf
```

Every report manifests its UTC generation time, exact source timestamps and
windows, source status, source digest, rule versions, selection frame, actual
coverage, recorded failures, and limitations in
`reports/latest/report-manifest.json`. Failures are recorded under
`data/derived/current-status.json`; a failed source never becomes a fresh
success. If PDF generation fails, `reports/latest/` remains the last valid
report and `reports/status.json` records the staleness.

The normal language metric is GitHub language **bytes**. It is not LOC:

- **equal-repository share** gives each measured repository equal weight and
  averages its internal language-byte shares;
- **byte-weighted share** sums bytes over the same measured repository panel.

Empty/unmeasured repositories are separately counted and excluded from both
denominators. An optional local-only LOC measurement is available when
[`cloc`](https://github.com/AlDanial/cloc) is installed:

```bash
github-account-analysis loc --repository-dir /path/to/local/checkout
```

It bounds file count, runs `cloc --json --by-file`, and never converts GitHub
language bytes into LOC.

## Attribution rules

The checked-in [`config/attribution-rules.v1.json`](config/attribution-rules.v1.json)
defines the only accepted evidence. Examples:

```text
Implement request handling

AI-Assisted: yes
AI-Tools: GitHub Copilot, Claude
```

```text
Implement request handling

Human-Only: yes
```

Accepted primary values are:

- **Explicitly AI-assisted**: a valid AI declaration or configured exact
  co-author declaration.
- **Explicitly exclusively human-declared**: a valid human-only declaration.
- **Indeterminate**: missing, malformed, conflicting, unsupported, or any
  other evidence.

Commit-message mentions of AI tools, code appearance, timestamps, and arbitrary
co-authors are not evidence. Multiple named tools count once in the primary
AI-assisted category, and appear individually only in the secondary
multi-label tool breakdown. Bot identities ending in `[bot]` are a separate
dimension and are not called AI-assisted.

## Data methodology and limits

GitHub's [Search API](https://docs.github.com/rest/search/search) has a
1,000-result cap and rate limits, so it cannot establish an authoritative
global repository census. [GH Archive](https://www.gharchive.org/) is an
observed public event stream. `PushEvent` payload commit objects are neither a
complete commit census nor equal to pushes. Public historical datasets and API
metadata are bounded/stale populations.

The scheduled workflow samples one completed UTC event hour each day and scopes
that event ingestion to the latest configured repository panel, which bounds
runtime and storage. It is therefore also subject to panel selection,
time-of-day, availability, declaration, tool-reporting, and survivorship bias.
Missing months in a trend are not fabricated as zeroes. The chart only
aggregates observed commit facts in source windows within the preceding ten
years.

Event ingestion defaults to at most 50,000 retained commit objects and a 1 GB
DuckDB per-connection memory ceiling. It records an explicit source failure
rather than silently truncating or publishing a partial fresh observation; an
operator can only raise the cap deliberately with `--max-commits`.
Detailed event facts and repository observations outside the closed ten-year
report window through the current generation time are pruned transactionally;
source provenance/status remains so the retained state does not masquerade as
complete history. The active event population is identified by its
analysis-schema revision, rule version, and normalized repository filter.
Changing any of these starts a new observed trend population; older
observations and their attribution facts are not silently combined with it.
If a pre-selection attribution table is found, startup preserves it as
`event_commit_attributions_legacy` and starts an empty selection-specific
attribution table; re-ingest source inputs rather than assigning ambiguous
legacy classifications to a newer selection.

`DuckDB` keeps local incremental state. Derived Parquet snapshots and status
are committed after successful runs so the project has durable history. Raw
event archives remain untracked ephemeral inputs; the retained source URL,
digest, UTC window, rule version, configuration, and row counts make the
processed observation reproducible. Review the terms and licenses for
[GitHub's API](https://docs.github.com/site-policy/github-terms/github-terms-of-service),
[GitHub REST API](https://docs.github.com/rest), GH Archive, and every
downstream dataset before redistributing data. Do not store private,
credential-bearing, or sensitive content in tracked results.

## GitHub Actions and Pages

[`.github/workflows/collect-and-publish.yml`](.github/workflows/collect-and-publish.yml)
runs daily in UTC and on manual dispatch. It has concurrency control, job time
limits, retries and timeouts for downloading the event file, bounded commit
ingest, minimal job-specific permissions, and never echoes `GITHUB_TOKEN`.
It records a failed source status but only publishes Pages after a newly
ingested source produces and passes a PDF validation check.

To enable GitHub Pages, repository administrators must enable **Pages** with
the **GitHub Actions** source in the repository settings. Availability depends
on repository/account visibility and Pages policy; this workflow does not
create secrets or bypass those settings.

## Development

```bash
python -m pip install -e '.[test]'
pytest
```

Tests cover attribution boundaries, SHA/repository-observation deduplication,
replay idempotence, source failures, UTC/month aggregation, language
denominators, synthetic end-to-end output, and PDF structure. The project uses
the locally pinned Engineering Agents workflow documented in `AGENTS.md`.
