# Engineering context

## Product

This project produces bounded, reproducible analysis of **public** GitHub data.
It intentionally reports declared attribution rather than attempting to prove
whether a person or an AI originated code. The deliverable has two separately
labelled populations:

1. A deterministic, configured repository panel used for repository metadata
   and GitHub language-byte analysis.
2. An observed public-event stream, normally GH Archive-like `PushEvent`
   records, used for activity and declared-attribution trends.

Neither population is an all-GitHub census or assumed representative.

## Stack and boundaries

- Python 3.9+ package under `src/github_account_analysis/`
- DuckDB is the durable analytical state store; derived tabular snapshots are
  Parquet.
- The GitHub REST client uses Python's standard library so authentication is
  optional and no unused HTTP dependency is added.
- Jinja2 and WeasyPrint create one semantic HTML/PDF report; Matplotlib creates
  an SVG trend chart.
- `pytest` is the test runner; `pypdf` is only a test dependency used for PDF
  structural checks.

## Commands

After installation, the supported commands are:

```bash
python -m pip install -e '.[test]'
pytest
github-account-analysis collect-panel --config config/repository-panel.json
github-account-analysis ingest-events --input path/to/events.json.gz
github-account-analysis report
github-account-analysis sample
```

Exact validation commands and outcomes are recorded in the final change
handoff. GitHub Actions runs the same CLI commands in a bounded Linux runner.

## Important constraints

- Attribute commits only through the versioned, explicit rules configuration.
  Missing evidence means `indeterminate`, never human-authored.
- Bot identities are a separate dimension. Bot activity is not classified as
  AI-assisted attribution.
- Collection and reporting timestamps are UTC and failure status is persistent.
  A failed input must not replace a valid report.
- The standard language metric is GitHub language **bytes**, not lines of code.
  Optional `cloc` analysis is deliberately separate and runs only when `cloc`
  is installed.
