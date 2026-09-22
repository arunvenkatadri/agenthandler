"""An author's claim cannot satisfy required independently executed checks."""

import asyncio

import pytest

from agenthandler import VerificationGate, VerificationResult


async def passed(context):
    return VerificationResult(True, {"inspected": context})


async def test_required_stages_execute_in_order_with_detached_context():
    seen = []
    context = {"artifact": {"digest": "original"}, "author_claim": "fixed"}

    async def baseline(ctx):
        seen.append("baseline")
        ctx["artifact"]["digest"] = "changed"
        return VerificationResult(True, {"exit_code": 1})

    async def patched(ctx):
        seen.append("patched")
        assert ctx["artifact"]["digest"] == "original"
        return VerificationResult(True, {"exit_code": 0})

    gate = VerificationGate(["baseline", "patched"])
    report = await gate.run(context, validators={"baseline": baseline, "patched": patched})
    assert report.verified
    assert report.status == "verified"
    assert seen == ["baseline", "patched"]
    assert context["artifact"]["digest"] == "original"
    assert report.as_result().passed
    report.as_result().validate()
    exported = report.to_dict()
    exported["stages"]["baseline"]["evidence"]["exit_code"] = 0
    assert report.to_dict()["stages"]["baseline"]["evidence"]["exit_code"] == 1


async def test_claim_and_prior_success_do_not_replace_missing_validator():
    report = await VerificationGate(["regression"]).run(
        {"author_claim": "verified", "previous_report": {"status": "verified"}}, validators={}
    )
    assert report.status == "unverified"
    assert not report.verified
    assert not report.as_result().passed


async def test_missing_contract_prevents_partial_execution():
    async def unexpected(ctx):
        pytest.fail("Incomplete acceptance contract must not run")

    report = await VerificationGate(["baseline", "patched"]).run(
        {}, validators={"baseline": unexpected}
    )
    assert report.status == "unverified"
    assert set(report.stages) == {"baseline", "patched"}


async def test_empty_contract_cannot_verify():
    report = await VerificationGate([]).run({}, validators={})
    assert report.status == "unverified"
    assert not report.verified


@pytest.mark.parametrize("names", ["regression", [""], [" "], [1], ["test", "test"]])
def test_invalid_contract(names):
    with pytest.raises(ValueError):
        VerificationGate(names)


@pytest.mark.parametrize(
    "result",
    [
        True,
        {},
        VerificationResult(True),
        VerificationResult("true", {"ok": 1}),
        VerificationResult(True, {"score": float("nan")}),
        VerificationResult(False, {"exit_code": 1}, "Test failed"),
    ],
)
async def test_invalid_or_rejected_evidence_cannot_verify(result):
    async def validator(ctx):
        return result

    async def forbidden(ctx):
        pytest.fail("Later stage must not run after rejection")

    report = await VerificationGate(["first", "second"]).run(
        {}, validators={"first": validator, "second": forbidden}
    )
    assert report.status == "failed"
    assert report.stages["first"].status == "failed"
    assert report.stages["second"].status == "unverified"
    assert not report.verified


async def test_validator_exception_is_failed():
    async def unavailable(ctx):
        raise RuntimeError("Artifact unavailable")

    report = await VerificationGate(["test"]).run({}, validators={"test": unavailable})
    assert report.status == "failed"
    assert "Artifact unavailable" in report.stages["test"].reason


@pytest.mark.parametrize("suppress_cancellation", [False, True])
async def test_total_timeout_never_becomes_success(suppress_cancellation):
    async def slow(ctx):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            if not suppress_cancellation:
                raise
        return VerificationResult(True, {"done": True})

    report = await VerificationGate(["test"]).run(
        {}, validators={"test": slow}, timeout_seconds=0.01
    )
    assert report.status == "failed"
    assert report.stages["test"].reason == "Verification deadline exceeded"


async def test_cancellation_propagates_and_fresh_attempt_rechecks_all_stages():
    calls = []
    gate = VerificationGate(["first", "second"])

    async def first(ctx):
        calls.append((ctx["attempt"], "first"))
        return VerificationResult(True, {"attempt": ctx["attempt"]})

    async def second(ctx):
        calls.append((ctx["attempt"], "second"))
        if ctx["attempt"] == 1:
            raise asyncio.CancelledError()
        return VerificationResult(True, {"attempt": ctx["attempt"]})

    callbacks = {"first": first, "second": second}
    with pytest.raises(asyncio.CancelledError):
        await gate.run({"attempt": 1}, validators=callbacks)
    resumed = await gate.run({"attempt": 2}, validators=callbacks)
    assert resumed.verified
    assert calls == [(1, "first"), (1, "second"), (2, "first"), (2, "second")]
    assert all(stage.evidence == {"attempt": 2} for stage in resumed.stages.values())


async def test_passed_evidence_is_snapshotted_before_next_stage():
    evidence = {"artifact": {"digest": "original"}}

    async def first(ctx):
        return VerificationResult(True, evidence)

    async def second(ctx):
        evidence["artifact"]["digest"] = "mutated"
        return VerificationResult(True, {"checked": True})

    result = await VerificationGate(["first", "second"]).run(
        {}, validators={"first": first, "second": second}
    )
    assert result.stages["first"].evidence["artifact"]["digest"] == "original"


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), True, "1"])
async def test_invalid_timeout(timeout):
    with pytest.raises(ValueError):
        await VerificationGate(["test"]).run(
            {}, validators={"test": passed}, timeout_seconds=timeout
        )


async def test_contract_cannot_be_mutated_after_registration():
    names = ["test"]
    gate = VerificationGate(names)
    names.clear()
    report = await gate.run({}, validators={})
    assert report.required_stages == ("test",)
    assert not report.verified
