"""Behaviour every MessageQueue must have, run against each implementation."""

from __future__ import annotations

import asyncio

import pytest

from providers.queue import InProcessQueue


@pytest.fixture(params=["in-process"])
def make_queue(request):
    return InProcessQueue


async def _wait_for(predicate, timeout=2.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met before timeout")
        await asyncio.sleep(0.01)


def test_published_messages_reach_subscriber_in_order(make_queue):
    async def scenario():
        queue = make_queue()
        received = []

        async def handler(payload):
            received.append(payload["n"])

        await queue.subscribe("jobs", handler)
        await queue.start()
        for n in range(3):
            await queue.publish("jobs", {"n": n})
        await _wait_for(lambda: len(received) == 3)
        await queue.stop()
        return received

    assert asyncio.run(scenario()) == [0, 1, 2]


def test_failing_handler_does_not_stop_delivery(make_queue):
    async def scenario():
        queue = make_queue()
        received = []

        async def handler(payload):
            if payload["n"] == 0:
                raise RuntimeError("boom")
            received.append(payload["n"])

        await queue.subscribe("jobs", handler)
        await queue.start()
        await queue.publish("jobs", {"n": 0})
        await queue.publish("jobs", {"n": 1})
        await _wait_for(lambda: received == [1])
        await queue.stop()

    asyncio.run(scenario())


def test_stop_is_safe_to_call_twice(make_queue):
    async def scenario():
        queue = make_queue()
        await queue.start()
        await queue.stop()
        await queue.stop()

    asyncio.run(scenario())
