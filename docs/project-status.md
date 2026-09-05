# What is verified

The integration branch combines the bug-scan fixes, verified completion, durable
task execution, and the local workbench. Validation on September 5, 2026:

- 916 Python tests passed, including three real Chromium/HTTP end-to-end tests.
- Seven dashboard JavaScript tests passed.
- Overall Python coverage: 83.26%.
- Ruff lint/format, strict mypy, wheel/sdist build and twine validation passed.
- The built wheel was installed into a separate Python 3.13 environment outside
  the source checkout; its CLI, packaged UI, and task API load successfully.

The reproducible user workflow is documented in [workbench.md](workbench.md).
It authenticates, computes an exact order total, saves and independently checks
the report, downloads it, and restores its state after restart. A browser test
kills the server between artifact creation and checkpoint persistence, then
resumes the same job without rewriting the artifact or resetting the budget.

The tests are deterministic and do not make paid model calls. They do not prove
live model behavior, performance under production load, or every external SDK,
database, and streaming provider integration. The older dashboard's LLM chat and
prompt builder still require an application backend. The workbench is a local
reference application, not a hosted multi-user service.

Review process: all changes still require passing CI and a Codex or Claude review
of the current integration commit. Owner approval is also required for PRs by
other authors. No review or branch-protection bypass is part of this integration.
