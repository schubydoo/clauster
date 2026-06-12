"""Unit tests for ``app.stream_until_disconnect`` (the ghost-WS-task guard).

A send-only WebSocket handler blocked on an idle event source never observes the
client's disconnect: the ASGI task lives forever, leaking its subscription and
stalling uvicorn's graceful shutdown ("Waiting for background tasks to
complete."). The helper races the stream against a receive loop so disconnect —
clean close, dropped transport, or the server's own shutdown close — always ends
the handler. Reproduced live on the dev instance 2026-06-12.
"""

from __future__ import annotations

import asyncio

import pytest

from clauster.app import stream_until_disconnect


class _FakeWS:
    """Minimal stand-in exposing the one method the helper uses: ``receive()``."""

    def __init__(self) -> None:
        self._incoming: asyncio.Queue[dict] = asyncio.Queue()

    async def receive(self) -> dict:
        return await self._incoming.get()

    def push(self, message: dict) -> None:
        self._incoming.put_nowait(message)


async def test_disconnect_ends_an_idle_stream():
    # The regression: the stream never produces anything (idle hosted session) —
    # the client disconnect alone must end the handler, promptly.
    ws = _FakeWS()
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def _stream() -> None:
        started.set()
        try:
            await asyncio.Event().wait()  # blocks forever
        except asyncio.CancelledError:
            cancelled.set()
            raise

    task = asyncio.ensure_future(stream_until_disconnect(ws, _stream))
    await asyncio.wait_for(started.wait(), timeout=1.0)
    ws.push({"type": "websocket.disconnect", "code": 1000})
    await asyncio.wait_for(task, timeout=1.0)
    assert cancelled.is_set()


async def test_stream_completion_ends_the_helper():
    ws = _FakeWS()
    sent: list[int] = []

    async def _stream() -> None:
        sent.extend([1, 2, 3])

    await asyncio.wait_for(stream_until_disconnect(ws, _stream), timeout=1.0)
    assert sent == [1, 2, 3]


async def test_stream_errors_propagate():
    ws = _FakeWS()

    async def _stream() -> None:
        raise RuntimeError("send after close")

    with pytest.raises(RuntimeError, match="send after close"):
        await asyncio.wait_for(stream_until_disconnect(ws, _stream), timeout=1.0)


async def test_client_chatter_is_ignored_until_disconnect():
    # A client that sends messages must not end the stream — only disconnect does.
    ws = _FakeWS()
    sent: list[int] = []
    started = asyncio.Event()
    proceed = asyncio.Event()
    advanced = asyncio.Event()

    async def _stream() -> None:
        sent.append(1)
        started.set()
        await proceed.wait()
        sent.append(2)
        advanced.set()
        await asyncio.Event().wait()  # then idle forever

    task = asyncio.ensure_future(stream_until_disconnect(ws, _stream))
    await asyncio.wait_for(started.wait(), timeout=1.0)
    ws.push({"type": "websocket.receive", "text": "ping"})
    proceed.set()
    # If the chatter had ended the helper, the stream would be cancelled before
    # appending 2 and this wait would time out.
    await asyncio.wait_for(advanced.wait(), timeout=1.0)
    assert not task.done()  # chatter did not end the helper
    assert sent == [1, 2]  # the stream kept running through the chatter
    ws.push({"type": "websocket.disconnect", "code": 1000})
    await asyncio.wait_for(task, timeout=1.0)
