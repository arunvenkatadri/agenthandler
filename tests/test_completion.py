"""Completion is an acceptance decision, never a truthy model response."""

import json

import pytest

from agenthandler import (
    CompletionStatus,
    MemoryStore,
    ReflectionLoop,
    SessionManager,
    VerificationResult,
)


async def run_claim(response, verifier=None):
    async def llm(prompt):
        return response

    return await ReflectionLoop(
        SessionManager(MemoryStore()), "test", "Create the artifact", llm, {}, verifier=verifier
    ).run()


@pytest.mark.parametrize(
    "response",
    [
        "garbage",
        "{}",
        "null",
        "[]",
        '"done"',
        "true",
        "42",
        '{"tool": null, "done": false}',
        '{"done": "true"}',
        '{"done": 1}',
        '{"done": true, "tool": "write"}',
        '{"done": true, "final_answer": []}',
        '{"done": false, "tool": []}',
        '{"done": false, "tool": "write", "args": []}',
    ],
)
async def test_invalid_claim_never_completes(response):
    result = await run_claim(response)
    assert result.status == CompletionStatus.INVALID
    assert not result.completed
    assert result.stopped_reason == "invalid_response"


async def test_unverified_claim_is_only_proposed():
    result = await run_claim('{"done": true, "final_answer": "All done"}')
    assert result.status == CompletionStatus.PROPOSED
    assert not result.completed
    assert result.final_answer == "All done"
    assert result.cycles[0].goal_progress != 1.0


async def test_explicit_blocked_work():
    result = await run_claim(
        '{"done": false, "blocked": true, "tool": null, "reason": "Need access"}'
    )
    assert result.status == CompletionStatus.BLOCKED
    assert not result.completed
    assert result.stopped_reason == "Need access"


@pytest.mark.parametrize("passed", [True, False])
async def test_validator_controls_completion(passed):
    async def verify(proposal):
        assert proposal.final_answer == "artifact.txt"
        assert proposal.status == CompletionStatus.PROPOSED
        return VerificationResult(passed, {"artifact_exists": passed}, "Checked artifact")

    result = await run_claim('{"done": true, "final_answer": "artifact.txt"}', verify)
    assert result.completed is passed
    assert result.status == (CompletionStatus.VERIFIED if passed else CompletionStatus.BLOCKED)
    assert result.verification.evidence == {"artifact_exists": passed}


@pytest.mark.parametrize(
    "verification",
    [
        True,
        {},
        VerificationResult(True),
        VerificationResult("true", {"ok": True}),
        VerificationResult(True, {"score": float("nan")}),
    ],
)
async def test_invalid_verifier_cannot_certify_completion(verification):
    async def verify(proposal):
        proposal.completed = True
        return verification

    result = await run_claim('{"done": true}', verify)
    assert not result.completed
    assert result.status == CompletionStatus.INVALID


async def test_validator_failure_is_not_success():
    async def verify(proposal):
        raise RuntimeError("artifact unavailable")

    result = await run_claim('{"done": true}', verify)
    assert not result.completed
    assert "artifact unavailable" in result.stopped_reason


@pytest.mark.parametrize("stop, expected", [(True, "blocked"), ("false", "invalid")])
async def test_reflection_stop_is_not_completion(stop, expected):
    async def llm(prompt):
        if "What should you do next" in prompt:
            return '{"done": false, "tool": "noop"}'
        if "Reflect on this cycle" in prompt:
            return json.dumps({"should_stop": stop, "reason": "Goal unreachable"})
        return "observed"

    async def noop():
        return "ok"

    result = await ReflectionLoop(
        SessionManager(MemoryStore()), "test", "goal", llm, {"noop": noop}
    ).run()
    assert not result.completed
    assert result.status == expected
