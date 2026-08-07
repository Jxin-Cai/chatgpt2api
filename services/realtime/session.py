from __future__ import annotations

import asyncio
import base64
import json
import time
from typing import Any

from fastapi import WebSocket

from services.realtime.audio_track import BufferedAudioStreamTrack, SAMPLE_RATE, FRAME_BYTES
from services.realtime.chatgpt_webrtc import create_peer_connection
from utils.log import logger


class RealtimeSession:
    """管理一个实时语音会话的完整生命周期。

    职责：
    - 建立 WebRTC 连接到 ChatGPT
    - 桥接客户端 WebSocket ↔ WebRTC 音频/事件
    """

    def __init__(self, identity: dict, model: str, websocket: WebSocket, access_token: str):
        self._identity = identity
        self._model = model
        self._ws = websocket
        self._access_token = access_token
        self._pc = None
        self._input_track: BufferedAudioStreamTrack | None = None
        self._data_channel = None
        self._voice = "ember"
        self._closed = False
        self._tasks: list[asyncio.Task] = []
        self._start_time = time.time()

    async def run(self) -> None:
        """主运行循环 — 建立连接后并发处理 WS 读/写。"""
        try:
            await self._start()
            await self._send_event("session.created", {
                "session": {"id": self._location, "model": self._model, "voice": self._voice}
            })

            reader_task = asyncio.create_task(self._client_reader(), name="ws-reader")
            sender_task = asyncio.create_task(self._audio_sender(), name="audio-sender")
            dc_task = asyncio.create_task(self._dc_reader(), name="dc-reader")
            self._tasks = [reader_task, sender_task, dc_task]

            done, _ = await asyncio.wait(self._tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                if task.exception() and not self._closed:
                    logger.warning(f"[realtime] Task {task.get_name()} error: {task.exception()}")
        except Exception as e:
            if not self._closed:
                await self._send_error(str(e))
                logger.error(f"[realtime] Session error: {e}")
        finally:
            await self.close()

    async def _start(self) -> None:
        """建立 WebRTC 连接。"""
        self._pc, self._input_track, self._data_channel, remote_audio, self._location = await create_peer_connection(
            access_token=self._access_token,
            voice=self._voice,
        )
        self._remote_audio_track = remote_audio

        self._connection_ready = asyncio.Event()

        @self._pc.on("connectionstatechange")
        async def on_state():
            state = self._pc.connectionState
            logger.info(f"[realtime] WebRTC state: {state}")
            if state == "connected":
                self._connection_ready.set()
            elif state in ("failed", "closed"):
                self._connection_ready.set()

        @self._pc.on("track")
        def on_track(track):
            if track.kind == "audio":
                self._remote_audio_track = track

        # 等待连接建立
        try:
            await asyncio.wait_for(self._connection_ready.wait(), timeout=15)
        except asyncio.TimeoutError:
            raise RuntimeError(f"WebRTC connection timeout (state={self._pc.connectionState})")

        if self._pc.connectionState != "connected":
            raise RuntimeError(f"WebRTC connection failed (state={self._pc.connectionState})")

        logger.info("[realtime] WebRTC connected")

    async def _client_reader(self) -> None:
        """从客户端 WebSocket 读取事件并处理。"""
        while not self._closed:
            try:
                raw = await self._ws.receive_text()
            except Exception:
                break
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                await self._send_error("Invalid JSON")
                continue

            event_type = event.get("type", "")
            await self._handle_client_event(event_type, event)

    async def _handle_client_event(self, event_type: str, event: dict) -> None:
        if event_type == "input_audio_buffer.append":
            audio_b64 = event.get("audio", "")
            if audio_b64 and self._input_track:
                try:
                    pcm_data = base64.b64decode(audio_b64)
                    self._input_track.push_pcm16(pcm_data)
                except Exception:
                    pass

        elif event_type == "input_audio_buffer.commit":
            pass  # VAD 由 ChatGPT 服务端处理

        elif event_type == "input_audio_buffer.clear":
            pass

        elif event_type == "session.update":
            session = event.get("session", {})
            if "voice" in session:
                self._voice = session["voice"]
            await self._send_event("session.updated", {"session": {"voice": self._voice}})

        elif event_type == "response.cancel":
            if self._data_channel and self._data_channel.readyState == "open":
                self._data_channel.send(json.dumps({"type": "response.cancel"}))

    async def _audio_sender(self) -> None:
        """从 ChatGPT 的远端音频轨道读取帧，编码为 base64 发送给客户端。"""
        track = getattr(self, "_remote_audio_track", None)
        if not track:
            # 等待远端 track 到达
            for _ in range(50):
                await asyncio.sleep(0.1)
                track = getattr(self, "_remote_audio_track", None)
                if track:
                    break
            if not track:
                logger.warning("[realtime] No remote audio track received")
                return

        silence_count = 0
        while not self._closed:
            try:
                frame = await asyncio.wait_for(track.recv(), timeout=5)
            except asyncio.TimeoutError:
                continue
            except Exception:
                break

            pcm_bytes = bytes(frame.planes[0])
            # 检测是否静音帧（全零或极低能量）
            is_silence = all(b == 0 for b in pcm_bytes[:20])

            if is_silence:
                silence_count += 1
                if silence_count > 25:  # 500ms 静音不推送
                    continue
            else:
                if silence_count > 25:
                    await self._send_event("response.audio.done", {})
                silence_count = 0
                audio_b64 = base64.b64encode(pcm_bytes).decode("ascii")
                await self._send_event("response.audio.delta", {"delta": audio_b64})

    async def _dc_reader(self) -> None:
        """读取 DataChannel 消息并转发给客户端。"""
        dc = self._data_channel
        if not dc:
            return

        message_queue: asyncio.Queue[str] = asyncio.Queue()

        @dc.on("message")
        def on_msg(msg):
            if isinstance(msg, str):
                try:
                    message_queue.put_nowait(msg)
                except asyncio.QueueFull:
                    pass

        while not self._closed:
            try:
                msg = await asyncio.wait_for(message_queue.get(), timeout=2)
            except asyncio.TimeoutError:
                continue
            except Exception:
                break

            try:
                data = json.loads(msg)
                event_type = data.get("type", "datachannel.message")
                await self._send_event(event_type, data)
            except json.JSONDecodeError:
                await self._send_event("datachannel.message", {"raw": msg[:1000]})

    async def _send_event(self, event_type: str, data: dict) -> None:
        if self._closed:
            return
        payload = {"type": event_type, **data}
        try:
            await self._ws.send_text(json.dumps(payload))
        except Exception:
            pass

    async def _send_error(self, message: str) -> None:
        await self._send_event("error", {"error": {"message": message}})

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True

        for task in self._tasks:
            if not task.done():
                task.cancel()

        if self._pc:
            try:
                await self._pc.close()
            except Exception:
                pass

        duration = time.time() - self._start_time
        logger.info(f"[realtime] Session closed after {duration:.1f}s")
