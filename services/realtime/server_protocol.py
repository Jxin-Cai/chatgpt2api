"""WebSocket transport with serialized backpressure for PCM and keepalive."""
import asyncio

from uvicorn.protocols.websockets.websockets_impl import WebSocketProtocol


class RealtimeWebSocketProtocol(WebSocketProtocol):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._write_drain_lock = asyncio.Lock()

    async def drain(self) -> None:
        # websockets 17 legacy has a single flow-control waiter, but PCM sends
        # and keepalive pings can both await it while a slow socket is paused.
        # Keep backpressure (and its bounded audio queues) without the second
        # writer asserting and killing an otherwise healthy voice session.
        async with self._write_drain_lock:
            await super().drain()
