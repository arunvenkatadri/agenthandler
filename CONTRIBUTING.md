# Contributing to AgentHandler

Thanks for your interest in contributing.

## Setup

```bash
git clone https://github.com/arunvenkatadri/agenthandler.git
cd agenthandler
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

### Changelog

Add an entry under `## Unreleased` in `CHANGELOG.md` for any user-visible change.
Call out breaking changes to public APIs (anything exported from `agenthandler`,
the REST control plane, policy keys) explicitly.

## Review and merging

PRs need passing CI (tests on Python 3.10–3.13, lint, formatting, type checking
and the package build) and a maintainer's approval. Resolve review conversations
before merging.

## Project structure

```
agenthandler/       Python library (zero dependencies)
tests/              Test suite (pytest)
examples/           Runnable examples
docs/               Guides and design notes
dashboard.html      Agent management UI (single-file, no build step)
npm/                npm placeholder that points to the Python package
```

- **`agenthandler/`** — the core library. Skills are capabilities (tool collections). Agents use skills and are supervised by policies.
- **`dashboard.html`** — standalone UI for managing agents, skills, and conversations. Pure HTML/CSS/JS, no build tools. Open directly in a browser.

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
