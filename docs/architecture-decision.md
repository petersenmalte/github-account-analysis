# ADR-001: Evidence-bound public profile analysis and safe quality-tool integration

**Status:** accepted  
**Decision date:** 2026-09-24

## Context and decision

The application must analyze public GitHub evidence without executing untrusted
target code or claiming code quality from account metadata. It therefore
separates four layers:

1. an anonymous, cached, rate-limited GitHub REST collector;
2. account-first commit/PR attribution and public artifact metrics;
3. provenance, partiality, configuration, and tool-version records; and
4. an optional, separately configured static-analysis job for repository state.

The interactive profile workflow does **not** clone repositories, run tests, or
run analyzers. Its report labels security, maintainability/complexity,
duplication, rule violations, and coverage as `not measured`, not zero. It also
does not equate repository-state measurements with evidence in attributable
changes.

For a historical analysis, an operator must choose and record representative
states (for example a baseline, a release boundary, and current default branch)
before running an approved scanner. Each state must use the same pinned scanner
version, rules/configuration, source inclusion/exclusion policy, and resource
limits. Report changed languages/composition separately from within-repository
evolution. This avoids both an unsafe every-commit scan and unsupported causal
claims about AI.

## Tool research and selection

The following maintained, official documentation was consulted on 2026-09-24:

| Tool | Appropriate dimensions | Local/CI/historical behavior | Reproducibility, license/cost/access | False-positive handling and decision |
|---|---|---|---|---|
| [CodeQL](https://docs.github.com/en/code-security/concepts/code-scanning/codeql/codeql-code-scanning) | Security vulnerabilities/errors across C/C++, C#, Go, Java/Kotlin, JavaScript/TypeScript, Python, Ruby, Rust, Swift, and Actions | GitHub documents default/advanced Actions setup and direct CLI operation; database builds may be required | Pin CodeQL action/CLI and query suite; GitHub availability and Advanced Security entitlements vary | Triage alerts against source/version; record query suite and unsupported languages. Preferred security analyzer when a safe CI snapshot can be built. |
| [Semgrep](https://semgrep.dev/docs/) | Static security rules and rule violations; language/rule coverage depends on selected rules | CLI can run locally or in CI without running target tests | Open-source CLI/rules can support no-paid-service workflows; pin CLI and rule revisions | Rules are heuristic. Preserve rule IDs, versions, paths, and findings; never turn absence of findings into “secure.” Preferred quick static scan for safe snapshot content. |
| [SonarQube](https://docs.sonarsource.com/sonarqube-server/analyzing-source-code/overview/) | Maintainability/complexity, duplication, rule violations, security, coverage import | Scanner is intended for local/CI project analysis; documentation notes full SCM history for SCM/blame features | Community/paid editions and hosted offerings have different capabilities; pin scanner/server/rules and import coverage explicitly | Quality gates and findings are configuration-dependent. Use only to report repository state, with coverage “not measured” unless an actual coverage import exists. |

No detector, coding style, commit mention, or generic coauthor is evidence of
AI use. The report accepts only a defined `AI-Classification: A` (explicitly
AI-assisted) or `AI-Classification: B` (explicitly entirely human declared)
trailer in attributable commit metadata or attributable PR metadata. It reports
class C undetermined with the same prominence and does not claim an
AI-written-code percentage.

## Consequences

* The default application works without paid services or credentials.
* Results can be partial because public events are limited, repository history
  may be rewritten/transferred, identities can be unavailable, and request
  budgets/pagination are bounded.
* Future analyzer adapters must invoke recognized tools (rather than a
  homegrown score), run in an isolated resource/network-limited environment,
  emit their full versions/configuration, and never execute target tests/builds
  by default.
