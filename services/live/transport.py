from __future__ import annotations

import asyncio
import base64
import json
import os
from collections.abc import Callable

from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCSessionDescription
from av import AudioResampler

from services.live.protocol import MAX_MESSAGE_BYTES
from services.realtime.audio_track import BufferedAudioStreamTrack
from services.realtime.session import resample_to_pcm16_mono
from utils.log import logger


class WebRTCTransport:
    """Terminate browser WebRTC so the data channel contains only public Live events.

    The other peer belongs to RealtimeSession and speaks the private provider protocol.
    """
    def __init__(self):
        configured_ice = os.getenv("CHATGPT2API_LIVE_ICE_SERVERS")
        if configured_ice:
            servers = json.loads(configured_ice)
            if not isinstance(servers, list):
                raise RuntimeError("Invalid Live ICE server configuration")
            self.pc = RTCPeerConnection(RTCConfiguration(iceServers=[RTCIceServer(**item) for item in servers]))
        else:
            self.pc = RTCPeerConnection()
        self.output = BufferedAudioStreamTrack(queue_max=15, prefill_frames=3, smooth_edges=True)
        self.pc.addTrack(self.output)
        self.channel = None
        self._ready = asyncio.Event()
        self._closed = False
        self._commands: asyncio.Queue[str | None] = asyncio.Queue(maxsize=128)
        self._tasks: set[asyncio.Task] = set()
        self._disconnect_task: asyncio.Task | None = None
        self.on_audio: Callable[[bytes], None] | None = None
        self.on_disconnect: Callable[[], None] | None = None

        @self.pc.on("datachannel")
        def datachannel(channel):
            if self.channel is not None or channel.label != "oai-events":
                channel.close()
                return
            self.channel = channel
            channel.bufferedAmountLowThreshold = 64_000

            @channel.on("open")
            def opened():
                self._ready.set()

            @channel.on("message")
            def message(raw):
                if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_MESSAGE_BYTES or self._commands.full():
                    self._lost()
                    return
                self._commands.put_nowait(raw)

            @channel.on("close")
            def closed():
                self._lost()

            if channel.readyState == "open":
                self._ready.set()

        @self.pc.on("track")
        def track(track):
            if track.kind == "audio":
                task = asyncio.create_task(self._read_audio(track))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)

        @self.pc.on("connectionstatechange")
        def state():
            if self.pc.connectionState in {"failed", "closed"}:
                self._lost()
            elif self.pc.connectionState == "disconnected" and self._disconnect_task is None:
                self._disconnect_task = asyncio.create_task(self._expire_disconnect())
            elif self.pc.connectionState == "connected" and self._disconnect_task:
                self._disconnect_task.cancel()
                self._disconnect_task = None

    async def answer(self, sdp: str) -> str:
        await self.pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type="offer"))
        if not any(t.kind == "audio" for t in self.pc.getTransceivers()) or "m=application" not in sdp:
            raise ValueError("Offer requires audio and an oai-events data channel")
        await self.pc.setLocalDescription(await self.pc.createAnswer())
        return self.pc.localDescription.sdp

    async def wait_ready(self) -> None:
        await asyncio.wait_for(self._ready.wait(), timeout=20)
        if self._closed or not self.channel or self.channel.readyState != "open":
            raise ConnectionError("Live data channel did not open")

    async def _read_audio(self, track) -> None:
        resampler = AudioResampler(format="s16", layout="mono", rate=48000)
        try:
            while not self._closed:
                frame = await track.recv()
                for _, pcm in resample_to_pcm16_mono(resampler, frame):
                    if self.on_audio:
                        self.on_audio(pcm)
        except asyncio.CancelledError:
            raise
        except Exception:
            if not self._closed:
                self._lost()

    async def _expire_disconnect(self) -> None:
        await asyncio.sleep(15)
        if self.pc.connectionState != "connected":
            self._lost()

    def _lost(self) -> None:
        self._ready.set()
        if self.on_disconnect:
            self.on_disconnect()
        if not self._commands.full():
            self._commands.put_nowait(None)

    async def receive_text(self) -> str:
        raw = await self._commands.get()
        if raw is None:
            raise ConnectionError("Browser disconnected")
        return raw

    async def send_text(self, raw: str) -> None:
        data = json.loads(raw)
        if data["type"] == "session.output_audio.delta":
            self.output.push_pcm16(base64.b64decode(data["delta"]))
            return  # Media goes over RTP, never duplicated on the data channel.
        if not self.channel or self.channel.readyState != "open":
            raise ConnectionError("Browser event channel is unavailable")
        deadline = asyncio.get_running_loop().time() + 5
        while self.channel.bufferedAmount > 256_000:
            if asyncio.get_running_loop().time() >= deadline or self.channel.readyState != "open":
                raise ConnectionError("Browser event channel is congested")
            await asyncio.sleep(0.01)
        self.channel.send(raw)
        if data["type"] == "session.closed":
            # Allow the terminal event to leave SCTP before closing the peer.
            while self.channel.bufferedAmount and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.01)

    def write_audio(self, pcm: bytes) -> None:
        if not self._closed:
            self.output.push_pcm16(pcm)

    def clear_audio(self) -> None:
        self.output.clear()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._disconnect_task:
            self._disconnect_task.cancel()
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        logger.info(
            f"[live-audio] output underflows={self.output.underrun_events}, "
            f"dropped_frames={self.output.dropped_frames}"
        )
        self.output.stop()
        await self.pc.close()
