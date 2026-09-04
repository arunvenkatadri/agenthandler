# Contributing to AgentHandler

Thanks for your interest in contributing.

## Setup

```bash
git clone https://github.com/arunvenkatadri/AgentHandler.git
cd AgentHandler
pip install -e ".[dev,server]"
```

## Running checks

```bash
# Tests
pytest tests/ -v

# Include the coverage check used in CI
pytest tests/ --cov=agenthandler --cov-fail-under=75

# Lint
ruff check .
ruff format --check .

# Type check
mypy agenthandler/
```

All three must pass before submitting a PR.

## Making changes

1. Fork the repo and create a branch from `main`.
2. Write tests for any new functionality.
3. Make sure all checks pass.
4. Open a PR with a clear description of what changed and why.

## Merge reviews

All PRs must pass tests on Python 3.10–3.13, lint, formatting, type checking,
package validation, and the `Review policy` status check. New commits require
new reviews. Resolve review conversations before merging.

- PRs authored by `arunvenkatadri`: a Codex or Claude review of the current commit.
- PRs authored by anyone else: that AI review plus `arunvenkatadri`'s approving
  GitHub review of the current commit.

The author determines the requirement, regardless of who clicks Merge.
The policy accepts submitted GitHub reviews from `chatgpt-codex-connector[bot]`
or `claude[bot]`. A pending/dismissed review, an old commit's review, a regular
comment, or an emoji reaction does not count. Requested changes must be cleared
by a fresh review from that reviewer. A Codex comment-only *submitted review*
counts as a completed review; resolve its findings before merging.

Paid reviews never run automatically. Only Arun can manually dispatch
`Claude PR Review` on `main`, and the `ai-review` environment requires Arun's
approval before releasing its key. The deterministic merge gate uses no model
API or paid secret. See [GitHub setup](docs/github-review-setup.md) for activation
and credential configuration.

## Project structure

```
agenthandler/           Python library (zero dependencies)
tests/              Test suite (pytest)
examples/           Runnable examples
dashboard.html      Agent management UI (single-file, no build step)
openclaw-plugin/    TypeScript plugin template for OpenClaw
```

- **`agenthandler/`** — the core library. Skills are capabilities (tool collections). Agents use skills and are supervised by policies.
- **`dashboard.html`** — standalone UI for managing agents, skills, and conversations. Pure HTML/CSS/JS, no build tools. Open directly in a browser.
- **`openclaw-plugin/`** — TypeScript plugin that bridges agenthandler to OpenClaw.

## Design principles

- **Zero dependencies.** Everything in `agenthandler/` uses only the Python standard library. If you need an external package, it goes in an example or optional integration.
- **Framework agnostic.** AgentHandler wraps tool calls — it doesn't own the agent loop. Don't add framework-specific code to the core.
- **Always return, never raise.** `Supervisor.call()` returns a `SupervisedResult`. Exceptions are for programming errors, not tool failures.
- **Thread safe by default.** Any shared state must be protected by a lock.
- **Agents and skills are separate.** Agents are supervised entities. Skills are reusable capabilities (tool collections) assigned to agents. Don't merge these concepts.

## Reporting issues

Open an issue on GitHub. Include:
- What you expected to happen
- What actually happened
- Minimal reproduction steps
- Python version and OS
