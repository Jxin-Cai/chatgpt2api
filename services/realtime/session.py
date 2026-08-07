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

        # 等待 DataChannel 打开后发送 track_state 激活 ChatGPT VAD
        for _ in range(50):
            if self._data_channel and self._data_channel.readyState == "open":
                break
            await asyncio.sleep(0.1)

        if self._data_channel and self._data_channel.readyState == "open":
            track_state_msg = json.dumps({
                "type": "data_message",
                "data": json.dumps({
                    "type": "track_state",
                    "payload": {
                        "type": "track_state",
                        "track_id": "microphone",
                        "media_type": "audio",
                        "media_source": "microphone",
                        "state": "live",
                    }
                })
            })
            self._data_channel.send(track_state_msg)
            logger.info("[realtime] Sent track_state to activate VAD")
        else:
            logger.warning(f"[realtime] DataChannel not open: {self._data_channel.readyState if self._data_channel else 'None'}")

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
                    if not hasattr(self, "_audio_log_count"):
                        self._audio_log_count = 0
                    self._audio_log_count += 1
                    if self._audio_log_count <= 3 or self._audio_log_count % 100 == 0:
                        logger.info(f"[realtime] Audio input: chunk={self._audio_log_count}, bytes={len(pcm_data)}, queue={self._input_track._queue.qsize()}")
                except Exception as e:
                    logger.error(f"[realtime] Audio decode error: {e}")

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
            self._send_to_dc({"type": "response.cancel"})

        elif event_type in ("conversation.item.create", "response.create",
                            "conversation.item.delete", "conversation.item.truncate"):
            self._send_to_dc(event)

    def _send_to_dc(self, event: dict) -> None:
        if self._data_channel and self._data_channel.readyState == "open":
            msg = json.dumps({"type": "data_message", "data": json.dumps(event)})
            self._data_channel.send(msg)

    async def _audio_sender(self) -> None:
        """从 ChatGPT 的远端音频轨道读取帧，编码为 base64 发送给客户端。"""
        track = getattr(self, "_remote_audio_track", None)
        if not track:
            for _ in range(50):
                await asyncio.sleep(0.1)
                track = getattr(self, "_remote_audio_track", None)
                if track:
                    break
            if not track:
                logger.warning("[realtime] No remote audio track received")
                return

        logger.info(f"[realtime] Audio sender started, remote track ready")
        recv_count = 0
        non_silence_count = 0
        silence_count = 0
        while not self._closed:
            try:
                frame = await asyncio.wait_for(track.recv(), timeout=5)
            except asyncio.TimeoutError:
                continue
            except Exception:
                break

            pcm_bytes = bytes(frame.planes[0])
            recv_count += 1
            # 用 RMS 能量判断是否静音（阈值 100，int16 范围 -32768~32767）
            import struct
            samples_check = struct.unpack(f'<{len(pcm_bytes)//2}h', pcm_bytes)
            rms = (sum(s*s for s in samples_check[:100]) / 100) ** 0.5
            is_silence = rms < 100
            if recv_count <= 3 or recv_count % 500 == 0:
                logger.info(f"[realtime] Remote audio frame #{recv_count}: rms={rms:.0f}, non_silence_total={non_silence_count}")

            if is_silence:
                silence_count += 1
                if silence_count > 25:  # 500ms 静音不推送
                    continue
            else:
                non_silence_count += 1
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
