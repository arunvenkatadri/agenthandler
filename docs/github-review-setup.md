# GitHub CI and review setup

## Activation

The workflow files must first be merged into `main`. The review-policy workflow
deliberately checks out `main`, so PR authors cannot replace the policy with an
always-pass script. For this first bootstrap PR, review the changes and merge
them before requiring the new `Review policy` check. Otherwise GitHub will wait
for a check whose trusted implementation is not installed yet.

After the bootstrap is merged, apply the versioned protection configuration:

```bash
gh api --method PUT repos/arunvenkatadri/agenthandler/branches/main/protection \
  --input .github/branch-protection.json
```

This configuration requires all eight checks from GitHub Actions (App ID 15368),
an up-to-date branch, a PR, and resolved conversations. It applies to admins too
and forbids force pushes and branch deletion. GitHub's blanket human approval
count stays at zero: the custom gate implements the author-specific approval
rule, avoiding a self-approval requirement on Arun's own PRs. `CODEOWNERS`
requests Arun's review; it does not implement the conditional gate on its own.

The gate refreshes on PR changes, submitted/edited/dismissed reviews, completed
Claude runs, and every ten minutes. A manual `Review policy` dispatch can refresh
one PR immediately. API failures leave the status pending. Fork review events
can have read-only tokens; the scheduled/default-branch refresh handles those.

## Spending controls

The old automatic Claude workflow is disabled in GitHub during bootstrap.
Do not re-enable it until the manual-only replacement is on `main`.

The `ai-review` environment is configured to allow only the `main` branch,
require `arunvenkatadri` as reviewer, and disable administrator bypass.
Paid reviews also check both `github.actor` and `github.triggering_actor` so
another collaborator cannot start a run or re-run Arun's previous job.

Store `ANTHROPIC_API_KEY` **only as an environment secret in `ai-review`**.
Do not put it in repository or organization secrets accessible to ordinary PR
workflows. Prefer a dedicated provider key with a provider-side spending limit.
No key is provisioned by this change and no paid review has been run.

To use Claude:

1. Install the official Claude GitHub App for this repository.
2. Add the environment secret as described above.
3. Once the manual workflow is on `main`, enable `Claude PR Review` in Actions.
4. Arun dispatches it with an open, ready PR number, then approves the environment.

Each run has a $2 CLI budget, 20-turn limit, and 15-minute job timeout. These
limit an authorized run; the provider-side limit is the final billing control.
The model reads the diff and source as data, with only Read/Glob/Grep tools.
It does not execute PR code. A deterministic step publishes the structured
result as a review on the captured commit and rejects a changed PR head.

Opening a PR or posting `@claude` cannot trigger this paid workflow. Contributors
can still cause ordinary CI runs. The repository is public and uses standard
GitHub-hosted Linux runners; no paid model participates in CI or the gate.

## Codex alternative

Codex's native GitHub integration is configured separately in the connected
ChatGPT account. This change does not enable its automatic reviews or alter its
usage/billing settings. Leave automatic reviews disabled unless their quota and
trigger behavior fit your requirements.

The gate accepts a standard Codex GitHub review associated with the exact PR
head commit. Codex sometimes reports a clean review with a thumbs-up reaction
instead; reactions cannot prove which commit was reviewed and do not satisfy
this gate. Obtain a submitted review or use the manual Claude review in that
case. Neither a human claiming to have used an AI nor a generic Actions bot
review is accepted.

References: [Codex GitHub reviews](https://learn.chatgpt.com/docs/third-party/github),
[Claude Code Action](https://github.com/anthropics/claude-code-action),
[Claude CLI limits](https://code.claude.com/docs/en/cli-reference), and
[GitHub branch protection](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches).
