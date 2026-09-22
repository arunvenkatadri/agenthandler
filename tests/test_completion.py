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


@pytest.mark.parametrize("kind", ["handle", "cyclic", "json"])
async def test_noncopyable_tool_output_cannot_prevent_independent_verification(tmp_path, kind):
    class Handle:
        def __deepcopy__(self, memo):
            raise TypeError("Live handles cannot be copied")

        def __str__(self):
            raise TypeError("Live handles must not be formatted")

    outputs = {"handle": Handle(), "json": {"items": [{"count": 1}]}}
    cycle = {"items": []}
    cycle["items"].append(cycle)
    outputs["cyclic"] = cycle
    output = outputs[kind]
    artifact = tmp_path / "report.txt"
    calls = []
    thinks = 0

    async def write_report(**kwargs):
        artifact.write_text("Required result")
        return output

    async def llm(prompt):
        nonlocal thinks
        if "What should you do next" in prompt:
            thinks += 1
            if thinks == 1:
                return '{"done": false, "tool": "write", "args": {"metadata": {"id": 1}}}'
            return '{"done": true, "final_answer": "Report written"}'
        if "Reflect on this cycle" in prompt:
            return '{"should_stop": false, "reflection": "Check the artifact"}'
        return "Observed report creation"

    async def verify(proposal):
        calls.append(proposal)
        assert proposal.status == CompletionStatus.PROPOSED
        assert proposal.cycles[0].tool_succeeded
        if kind == "handle":
            assert proposal.cycles[0].tool_output == "<non-JSON value omitted>"
        elif kind == "cyclic":
            assert proposal.cycles[0].tool_output == {"items": ["<cyclic value omitted>"]}
        else:
            assert proposal.cycles[0].tool_output == {"items": [{"count": 1}]}
            proposal.cycles[0].tool_output["items"][0]["count"] = 99
        proposal.cycles[0].tool_args["metadata"]["id"] = 99
        proposal.cycles[0].thought = "mutated"
        proposal.final_answer = "mutated"
        return VerificationResult(
            artifact.read_text() == "Required result", {"checked": str(artifact)}
        )

    result = await ReflectionLoop(
        SessionManager(MemoryStore()),
        "test",
        "Write report",
        llm,
        {"write": write_report},
        verifier=verify,
    ).run()
    assert len(calls) == 1
    assert result.completed
    assert result.final_answer == "Report written"
    assert result.cycles[0].tool_output is output
    assert result.cycles[0].tool_args == {"metadata": {"id": 1}}
    assert result.cycles[0].thought != "mutated"
    if kind == "json":
        assert output == {"items": [{"count": 1}]}


async def test_reflection_detaches_retained_verifier_evidence():
    evidence = {"artifact": {"digest": "checked", "tests": ["passed"]}}
    accepted = VerificationResult(True, evidence, "Checked artifact")

    async def verify(proposal):
        return accepted

    result = await run_claim('{"done": true, "final_answer": "artifact.txt"}', verify)
    evidence["artifact"]["tests"].clear()
    evidence["artifact"]["digest"] = "changed"
    evidence.clear()
    assert result.completed
    assert result.verification is not accepted
    assert result.verification.evidence == {"artifact": {"digest": "checked", "tests": ["passed"]}}
    result.verification.validate()
