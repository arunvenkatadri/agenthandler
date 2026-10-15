"""Supervisor.call_approved: a human approval authorizes exactly one matching call."""

from agenthandler import ApprovalQueue, Policy, Supervisor


async def echo(**kwargs):
    return kwargs


def gated_supervisor():
    queue = ApprovalQueue()
    sv = Supervisor(Policy(require_confirm=["send"]), session_id="s1", approval_queue=queue)
    return sv, queue


async def test_call_approved_runs_once_for_matching_approval():
    sv, queue = gated_supervisor()
    request = queue.submit("send", {"to": "a"}, "s1")
    queue.approve(request.approval_id, approved_by="ann")
    first = await sv.call_approved(request.approval_id, "send", echo, to="a")
    assert first.succeeded and first.output == {"to": "a"}
    again = await sv.call_approved(request.approval_id, "send", echo, to="a")
    assert again.error.kind == "approval_denied"


async def test_call_approved_rejects_mismatch_and_unapproved():
    sv, queue = gated_supervisor()
    request = queue.submit("send", {"to": "a"}, "s1")
    pending = await sv.call_approved(request.approval_id, "send", echo, to="a")
    assert pending.error.kind == "approval_denied"
    queue.approve(request.approval_id)
    wrong_args = await sv.call_approved(request.approval_id, "send", echo, to="b")
    assert wrong_args.error.kind == "approval_denied"
    plain = await sv.call("send", echo, to="a")
    assert plain.error.kind == "approval_pending"


async def test_call_with_tool_name_and_fn_args():
    """Regression: tool args named tool_name or fn must not collide with positional params."""
    sv = Supervisor(Policy())
    result = await sv.call("lookup", echo, tool_name="x", fn="y")
    assert result.succeeded and result.output == {"tool_name": "x", "fn": "y"}


async def test_call_approved_with_collision_args():
    """Regression: approved args including approval_id, tool_name, fn must not collide."""
    sv, queue = gated_supervisor()
    request = queue.submit("send", {"approval_id": "a", "tool_name": "t", "fn": "f"}, "s1")
    queue.approve(request.approval_id, approved_by="ann")
    result = await sv.call_approved(
        request.approval_id, "send", echo, approval_id="a", tool_name="t", fn="f"
    )
    assert result.succeeded and result.output == {"approval_id": "a", "tool_name": "t", "fn": "f"}


async def test_restored_rate_history_counts_against_the_limit():
    from agenthandler import ToolScope

    sv = Supervisor(Policy(tool_scopes=[ToolScope("ping", [], max_calls_per_minute=2)]))
    sv.restore_rate_history({"ping": [10.0, 30.0, 90.0]})  # 90s ago is outside the window
    assert (await sv.call("ping", echo)).error.kind == "rate_limited"
    fresh = Supervisor(Policy(tool_scopes=[ToolScope("ping", [], max_calls_per_minute=2)]))
    fresh.restore_rate_history({"ping": [10.0]})
    assert (await fresh.call("ping", echo)).succeeded
