# Run and verify the application locally

The packaged workbench exercises a complete, deterministic workflow: browser
authentication → task creation → supervised execution → saved report → independent
acceptance check → durable history. It requires no external application, API key
for a model, or billable model calls.

From a checkout containing this change:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[server]'
export AGENTHANDLER_API_KEY='choose-a-local-server-password'
.venv/bin/python -m agenthandler.workbench --data-dir .agenthandler
```

Open http://127.0.0.1:8000 and enter the same server password. Click **Create and
run** with the supplied CSV. The page should show **verified**, a **$12.60** total,
four reserved calls, and evidence for both milestones. Download the report and
compare it with the JSON file under `.agenthandler/reports/`.

Stop and restart the server with the same data directory. Reconnect and open the
saved job; its result and budget remain available. A verified job does not run
again. An interrupted job can be resumed using its existing job ID. Creating a
new job is a separate request with a new budget.

The workbench accepts at most 100 CSV rows and 10 KB of input. Its exact integer
cent arithmetic and file verification are deliberate: the first end-to-end
acceptance target is observable and reproducible. This proves the application
execution and recovery path, not the quality of a language model.

## Automated proof

```bash
.venv/bin/python -m pip install -e '.[dev,server,e2e]'
.venv/bin/python -m playwright install chromium
.venv/bin/python -m pytest tests/browser/ -q
```

The browser tests start real HTTP server processes and Chromium. They verify:

- Wrong credentials block access; a valid key enables task creation.
- Valid orders produce a checked, downloadable report with the expected total.
- Reloading the page restores the job without rewriting its artifact.
- A server process exits immediately after the report is saved but before its
  result checkpoint commits. A fresh server reconciles the receipt and completes
  the same job without changing the file or resetting the call budget.
- Invalid orders produce a visible error and create neither jobs nor reports.

The `browser` CI job installs Chromium and runs these tests. Ordinary Python CI
does not require browser dependencies. All these tests use local services only.

## Connect an application workflow

Register trusted `TaskTemplate` instances with `create_app(task_runner=...,
task_templates=[...])`. The runner and server must share a SessionManager.
Templates define the allowed callbacks, input validator, acceptance criteria,
and lifetime budget; clients cannot supply code or increase those limits.

The authenticated API provides `GET /task-templates`, `POST /tasks`, `GET /tasks`,
`GET /tasks/{id}`, and `POST /tasks/{id}/run`. A changed or missing template cannot
silently redefine an existing job. See [durable tasks](durable-tasks.md) for the
callback contract and provider-enforced token/cost reservations.

## Current product boundary

The older `dashboard.html` is a management and integration UI. Its chat, prompt
builder, and chat-based Deploy flow require an application backend implementing
`/chat` and `/generate-prompt`; the stock control-plane server does not implement
those endpoints. Use the workbench above for a runnable workflow included in
this repository. Connecting and validating a live model remains a separate
integration step. The Anthropic GitHub review federation rule is restricted to
the review workflow and is not a general runtime credential.

The stock library remains framework agnostic. No automatic paid model evaluation
or review trigger is added by this workbench.
