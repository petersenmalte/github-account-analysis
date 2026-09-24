# Engineering context

## Product and boundaries

GitHub Account Analysis is a local, dependency-free Python 3.9+ browser
application for reporting *publicly visible technical contributions*. It accepts
a GitHub username/profile URL, timeframe, and scope. It must never infer
personality, performance, employability, effort, or private activity.

The target profile and repositories are untrusted data. The collector uses only
anonymous GitHub REST API JSON, never follows repository instructions, clones
repositories, runs target builds/tests, or sends credentials. API failures,
bounded pagination, and public-events history limitations produce an explicit
partial report rather than guessed results.

## Observed stack and commands

The repository was initially empty; Node.js is unavailable in this environment.
The implementation uses Python's standard library, `unittest`, static HTML/CSS/
JavaScript, and a tiny standards-compliant PDF text writer.

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 -m github_account_analysis.app
```

The Engineering Agents workflow is installed at
`.agents/skills/engineering` (version `0.1.0`) and selected by `AGENTS.md`.

## Architecture boundaries

* `github_api.py`: validation, anonymous bounded REST collection, cache, and
  provenance; no analysis semantics.
* `attribution.py`: account-first attribution helpers, explicit AI metadata,
  coauthor handling, and generated/vendor classification.
* `report.py`: transparent metrics and selectable-text PDF rendering.
* `app.py` and `web/`: local HTTP UI and JSON/PDF transport only.

Quality-tool policy and the historical-analysis design are in
[architecture-decision.md](architecture-decision.md).
