# GitHub Account Analysis

A local web application that turns **public GitHub API evidence** into a
selectable-text PDF report and machine-readable JSON. It reports technical
artifacts and their provenance, not judgments about a person.

## Run

Python 3.9+ is the only runtime requirement.

```sh
PYTHONPATH=src python3 -m github_account_analysis.app
# Open http://127.0.0.1:8000
```

Enter a GitHub username or `https://github.com/<username>`, choose a timeframe,
and select owned repositories and/or contributions to other public repositories.
The result exposes the exact config, data-source URLs, partial-analysis reasons,
and local tool versions. Use **Download selectable-text PDF** for a report whose
text and source links are selectable/searchable.

To run the automated checks:

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

The repository's [CodeQL workflow](.github/workflows/codeql.yml) uses GitHub's
maintained CodeQL action to scan this application's Python source in CI. It is
not treated as an analysis of a requested target profile/repository.

## Methodology and limits

* Only anonymous GitHub REST API data is requested. No credentials, private
  data, external identity research, repository clone, target build, or target
  test is performed.
* Commit attribution requires the GitHub account on the API artifact. A matching
  name/email is never elevated to account attribution. The report distinguishes
  commit author, committer, and PR author; `Co-authored-by` shows shared
  coauthorship without invented individual shares.
* Owned repositories and contributed repositories are distinct. Identical commit
  object IDs are deduplicated (with the excluded item and reason retained).
  Generated/vendor files are separated from source change volume. Added/removed
  lines are change volume only, not productivity, effort, or unique authored
  code.
* GitHub public events are a limited recent history. Missing identities/emails,
  squash merges, transferred repositories, rewritten history, private activity,
  API failures, pagination caps, and unavailable metadata are explicitly
  uncertain or partial—not guessed.
* AI metadata classification is strict: only an `AI-Classification: A` or
  `AI-Classification: B` trailer in attributable commit/PR metadata qualifies.
  A is explicitly AI-assisted, B explicitly entirely human declared, and C is
  undetermined. Bots are separately counted. No style, detector, casual mention,
  or code percentage is used.
* Security, maintainability/complexity, duplication, rule violations, and
  coverage are separate fields and default to **not measured**, not zero. See
  [ADR-001](docs/architecture-decision.md) for maintained-tool research,
  historical comparability, reproducibility, licensing/access, and safe
  static-analysis policy.

## Example report

[`examples/public-profile-example-report.json`](examples/public-profile-example-report.json)
is a **synthetic, fixture-backed public-profile example**. It deliberately does
not represent measurements about any real account; use it to inspect the report
shape, provenance, classifications, and partial-report behavior offline.

## Reproducibility and operation

Responses include `schema_version`, generated timestamp, configuration, and
tool versions. The default cache directory is `.cache/github-account-analysis`;
set `GAA_CACHE_DIR` to move it. Collection has a 40-request budget, 12-second
HTTP timeout per request, process-wide 250ms request pacing, and cached API
responses keyed by full URL. Rate-limit response headers postpone subsequent
requests; the affected report remains visibly partial. Do not place the cache
under version control.

The supported no-cost path is this anonymous public-data workflow. Paid/hosted
quality alternatives are documented but neither purchased nor required.
