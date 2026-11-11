"""Exercise delivery failure and acknowledgement behavior without real brokers."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agenthandler.streams import KafkaStreamConnector, RedisStreamConnector


@pytest.mark.asyncio
async def test_kafka_retries_failed_record_before_committing_later_offsets(monkeypatch):
    events = []
    attempts = 0

    async def handler(payload):
        nonlocal attempts
        events.append(("handle", payload["offset"]))
        if payload["offset"] == 0:
            attempts += 1
            if attempts == 1:
                raise RuntimeError("temporary failure")

    class Consumer:
        offset = None

        def __aiter__(self):
            return self.messages()

        async def messages(self):
            for offset in [0, 1]:
                self.offset = offset
                yield SimpleNamespace(
                    key=b"key", value={}, topic="t", partition=0, offset=offset, timestamp=0
                )

        async def commit(self):
            events.append(("commit", self.offset))

    monkeypatch.setattr("agenthandler.streams.asyncio.sleep", AsyncMock())
    conn = KafkaStreamConnector("broker", "t", "g", handler)
    conn._consumer = Consumer()
    conn._running = True
    await conn._consume_loop()
    assert events == [("handle", 0), ("handle", 0), ("commit", 0), ("handle", 1), ("commit", 1)]


@pytest.mark.asyncio
async def test_redis_recovers_pending_and_retries_before_reading_new_messages(monkeypatch):
    attempts, acked = [], []
    pending = {"old": {"value": "original"}}
    conn = None

    async def handler(payload):
        attempts.append(payload["id"])
        if len(attempts) == 1:
            raise RuntimeError("temporary failure")

    async def read(**kwargs):
        if kwargs["streams"]["events"] == "0":
            return [("events", list(pending.items()))]
        conn._running = False
        return []

    async def ack(stream, group, message_id):
        acked.append(message_id)
        pending.pop(message_id)

    monkeypatch.setattr("agenthandler.streams.asyncio.sleep", AsyncMock())
    conn = RedisStreamConnector("redis://test", "events", "group", "same-consumer", handler)
    conn._redis = SimpleNamespace(
        xreadgroup=AsyncMock(side_effect=read), xack=AsyncMock(side_effect=ack)
    )
    conn._running = True
    await conn._consume_loop()
    assert attempts == ["old", "old"]
    assert acked == ["old"]
    assert not pending


@pytest.mark.asyncio
async def test_kinesis_retries_handler_before_advancing_to_next_record(monkeypatch):
    from agenthandler.streams import KinesisStreamConnector

    seen = []
    conn = None

    async def handler(payload):
        seen.append(payload["sequence_number"])
        if len(seen) == 1:
            raise RuntimeError("retry")
        if payload["sequence_number"] == "2":
            conn._running = False

    client = SimpleNamespace(
        describe_stream=AsyncMock(
            return_value={"StreamDescription": {"Shards": [{"ShardId": "s"}]}}
        ),
        get_shard_iterator=AsyncMock(return_value={"ShardIterator": "iterator"}),
        get_records=AsyncMock(
            return_value={
                "Records": [
                    {"Data": b"{}", "SequenceNumber": "1"},
                    {"Data": b"{}", "SequenceNumber": "2"},
                ],
                "NextShardIterator": "next",
            }
        ),
    )
    context = AsyncMock()
    context.__aenter__.return_value = client
    monkeypatch.setattr("agenthandler.streams.asyncio.sleep", AsyncMock())
    conn = KinesisStreamConnector("stream", handler)
    conn._session = SimpleNamespace(client=lambda *args, **kwargs: context)
    conn._running = True
    await conn._consume_loop()
    assert seen == ["1", "1", "2"]
