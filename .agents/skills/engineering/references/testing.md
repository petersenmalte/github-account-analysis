# Test/QA assignment

Use a separate QA agent when risk or integration uncertainty justifies a second
perspective: concurrency, migrations, authorization, complex state transitions,
cross-service behavior, or an explicit request for independent testing.

Map the acceptance criteria to observable behavior. Identify meaningful missing
cases from requirements and interfaces, not just the implementation's branches.
Prefer the existing test framework. Run targeted checks in an appropriate test
environment; avoid mutating shared or production data as part of validation.

Report reproducible failures, covered criteria and residual risks. Distinguish a
product defect from unavailable dependencies or a broken test environment. Include
commands actually run and outcomes. Adding tests requires explicit write ownership
from the main agent; otherwise return recommendations and evidence read-only.
