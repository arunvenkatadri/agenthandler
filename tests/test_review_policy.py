"""The merge gate must reject stale, missing, dismissed, and spoofed reviews."""

import importlib.util
import json
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "review_policy", ROOT / ".github/scripts/review_policy.py"
)
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)


def pr(author=policy.OWNER, head="current", draft=False):
    return {
        "number": 1,
        "user": {"login": author},
        "head": {"sha": head},
        "draft": draft,
        "state": "open",
        "base": {"ref": "main"},
    }


def review(login="claude[bot]", state="APPROVED", sha="current", id=1, type="Bot"):
    return {
        "id": id,
        "user": {"login": login, "type": type},
        "state": state,
        "commit_id": sha,
    }


@pytest.mark.parametrize("bot", sorted(policy.AI_REVIEWERS))
@pytest.mark.parametrize("state", ["APPROVED", "COMMENTED"])
def test_owner_needs_only_one_ai_review(bot, state):
    assert policy.evaluate(pr(), [review(bot, state)])[0] == "success"


def test_other_author_requires_owner_approval():
    reviews = [review()]
    assert policy.evaluate(pr("contributor"), reviews)[0] == "pending"
    reviews.append(review(policy.OWNER, id=2, type="User"))
    assert policy.evaluate(pr("contributor"), reviews)[0] == "success"


@pytest.mark.parametrize(
    "invalid",
    [
        review(sha="old"),
        review(state="PENDING"),
        review(state="DISMISSED"),
        review(type="User"),
        review(login="github-actions[bot]"),
        review(login="random[bot]"),
    ],
)
def test_invalid_ai_review_does_not_satisfy_gate(invalid):
    assert policy.evaluate(pr(), [invalid])[0] == "pending"


def test_missing_ai_and_draft_block_merge():
    assert policy.evaluate(pr(), [])[0] == "pending"
    assert policy.evaluate(pr(draft=True), [review()])[0] == "pending"


@pytest.mark.parametrize("state", ["DISMISSED", "CHANGES_REQUESTED"])
def test_newer_ai_decision_revokes_old_approval(state):
    reviews = [review(), review(state=state, id=2)]
    assert policy.evaluate(pr(), reviews)[0] != "success"
    # Input ordering must not resurrect the older approval.
    assert policy.evaluate(pr(), reviews[::-1])[0] != "success"


def test_new_commits_require_both_reviews_again():
    reviews = [review(), review(policy.OWNER, id=2, type="User")]
    assert policy.evaluate(pr("contributor", head="new"), reviews)[0] == "pending"
    reviews.append(review(sha="new", id=3))
    assert policy.evaluate(pr("contributor", head="new"), reviews)[0] == "pending"
    reviews.append(review(policy.OWNER, sha="new", id=4, type="User"))
    assert policy.evaluate(pr("contributor", head="new"), reviews)[0] == "success"


@pytest.mark.parametrize("state", ["DISMISSED", "CHANGES_REQUESTED"])
def test_owner_revocation_blocks_merge(state):
    reviews = [review(), review(policy.OWNER, id=2, type="User")]
    reviews.append(review(policy.OWNER, state=state, id=3, type="User"))
    assert policy.evaluate(pr("contributor"), reviews)[0] != "success"


def test_human_comment_does_not_revoke_approval():
    reviews = [review(), review(policy.OWNER, id=2, type="User")]
    reviews.append(review(policy.OWNER, state="COMMENTED", id=3, type="User"))
    assert policy.evaluate(pr("contributor"), reviews)[0] == "success"


def test_other_provider_cannot_override_requested_changes():
    reviews = [review(state="CHANGES_REQUESTED"), review("chatgpt-codex-connector[bot]", id=2)]
    assert policy.evaluate(pr(), reviews)[0] == "failure"


def test_head_change_during_evaluation_does_not_publish_success():
    api = Mock()
    api.request.side_effect = [pr(), pr(head="new")]
    api.pages.return_value = [review()]
    policy.run(api, [1])
    assert api.publish.call_count == 1
    assert api.publish.call_args.args[1] == "pending"


def test_api_error_invalidates_previous_success():
    api = Mock()
    api.request.return_value = pr()
    api.pages.side_effect = RuntimeError("API unavailable")
    with pytest.raises(RuntimeError):
        policy.run(api, [1])
    assert api.publish.call_args.args[1] == "pending"


def test_paginated_reviews_are_all_read(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "fake")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    api = policy.GitHub()
    api.request = Mock(side_effect=[[review(id=i) for i in range(100)], [review(id=101)]])
    assert len(api.pages("pulls/1/reviews")) == 101
    assert api.request.call_args.args[0].endswith("per_page=100&page=2")


def test_paid_workflow_cannot_be_triggered_by_prs_comments_or_other_actors():
    workflow = yaml.load(
        (ROOT / ".github/workflows/claude-pr-review.yml").read_text(), Loader=yaml.BaseLoader
    )
    assert set(workflow["on"]) == {"workflow_dispatch"}
    job = workflow["jobs"]["claude-review"]
    assert "github.actor == 'arunvenkatadri'" in job["if"]
    assert "github.triggering_actor == 'arunvenkatadri'" in job["if"]
    assert "github.ref == 'refs/heads/main'" in job["if"]
    assert job["environment"] == "ai-review"
    step = next(s for s in job["steps"] if s.get("id") == "claude")
    assert "--max-budget-usd 2" in step["with"]["claude_args"]


def test_review_gate_uses_only_trusted_base_code_and_no_paid_secrets():
    text = (ROOT / ".github/workflows/review-policy.yml").read_text()
    workflow = yaml.load(text, Loader=yaml.BaseLoader)
    checkout = workflow["jobs"]["verify"]["steps"][0]
    assert checkout["with"]["ref"] == "main"
    assert checkout["with"]["persist-credentials"] == "false"
    assert "secrets." not in text
    assert "anthropic" not in text.lower()


def test_federation_matches_only_the_protected_review_job():
    match = json.loads((ROOT / ".github/claude-federation-match.json").read_text())
    text = (ROOT / ".github/workflows/claude-pr-review.yml").read_text()
    workflow = yaml.load(text, Loader=yaml.BaseLoader)
    job = workflow["jobs"]["claude-review"]
    action = next(s for s in job["steps"] if s.get("id") == "claude")["with"]
    assert match["subject_prefix"] == (
        f"repo:arunvenkatadri/agenthandler:environment:{job['environment']}"
    )
    assert match["audience"] == action["anthropic_oidc_audience"]
    assert set(workflow["on"]) == {match["claims"]["event_name"]}
    assert match["claims"]["repository_id"] == "1186435851"
    assert match["claims"]["actor_id"] == match["claims"]["repository_owner_id"] == "16549185"
    assert match["claims"]["workflow_ref"] == (
        "arunvenkatadri/agenthandler/.github/workflows/claude-pr-review.yml@refs/heads/main"
    )
    assert match["claims"]["ref"] == "refs/heads/main"
    assert job["permissions"]["id-token"] == "write"
    for field in ("federation_rule_id", "organization_id", "service_account_id", "workspace_id"):
        assert action[f"anthropic_{field}"] == "${{ vars.ANTHROPIC_" + field.upper() + " }}"
    assert "anthropic_api_key" not in action
    assert "claude_code_oauth_token" not in action
    assert "secrets." not in text
