"""Compression must preserve the supervision result and fail open on compressor errors."""

import pytest

from agenthandler import compression
from agenthandler.policy import Policy


@pytest.fixture
def compressor(monkeypatch):
    calls = []

    def compress(messages, model):
        calls.append(messages)
        return [{"role": "user", "content": "compressed"}]

    monkeypatch.setattr(compression, "_headroom_available", True)
    monkeypatch.setattr(compression, "_headroom_compress", compress, raising=False)
    return calls


@pytest.mark.parametrize(
    "mode,output,compressed",
    [
        ("auto", "x" * 1000, True),
        ("auto", "short", False),
        ("always", "short", True),
        ("off", "x" * 1000, False),
    ],
)
async def test_success_metadata_survives_compression(compressor, mode, output, compressed):
    supervisor = compression.CompressedSupervisor(Policy(), compression=mode)

    async def tool():
        return output

    result = await supervisor.call("tool", tool)
    assert result.succeeded
    assert result.tool_name == "tool"
    assert result.error is None
    assert result.budget is not None
    assert result.output == ("compressed" if compressed else output)
    assert bool(compressor) is compressed
    assert result.user_message() == result.output


async def test_failure_is_not_compressed(compressor):
    supervisor = compression.CompressedSupervisor(Policy())

    async def tool():
        raise ValueError("failed")

    result = await supervisor.call("tool", tool)
    assert not result.succeeded
    assert result.error is not None
    assert not compressor


def test_compressor_failure_preserves_output(compressor, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("compression unavailable")

    monkeypatch.setattr(compression, "_headroom_compress", fail)
    output = {"data": "x" * 1000}
    assert compression.compress_output(output) is output
