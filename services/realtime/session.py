from __future__ import annotations

import asyncio
import base64
import json
import struct
import time
from collections.abc import Callable
from typing import Any

from av import AudioFrame, AudioResampler
from fastapi import WebSocket

from services.realtime.audio_track import BufferedAudioStreamTrack, SAMPLE_RATE, FRAME_BYTES
from services.realtime.chatgpt_webrtc import create_peer_connection
from utils.log import logger


class RealtimeQuotaExceeded(RuntimeError):
    """上游账号的实时语音额度已耗尽。"""


def decode_data_channel_message(message: str) -> dict[str, Any]:
    """解开 ChatGPT WebRTC 的 data_message 双层 JSON 封装。"""
    outer = json.loads(message)
    if outer.get("type") != "data_message":
        return outer
    inner = outer.get("data")
    if isinstance(inner, str):
        decoded = json.loads(inner)
        if isinstance(decoded, dict):
            return decoded
    if isinstance(inner, dict):
        return inner
    return outer


def quota_error_from_message(message: dict[str, Any]) -> str | None:
    payload = message.get("payload")
    if not isinstance(payload, dict):
        payload = {}

    if message.get("type") == "goodbye" and payload.get("reason") == "cap_reached":
        return str(payload.get("detail") or "realtime voice quota exhausted")

    if message.get("type") == "usage_update":
        rate_limit = payload.get("rate_limit_message")
        if isinstance(rate_limit, dict):
            exceeded = rate_limit.get("exceed_limit_message")
            if isinstance(exceeded, dict):
                title = str(exceeded.get("title") or "Daily Limit Reached")
                detail = str(exceeded.get("description_markdown") or "audio usage exceeded")
                return f"{title}: {detail}"
    return None


def resample_to_pcm16_mono(
    resampler: AudioResampler, frame: AudioFrame
) -> list[tuple[AudioFrame, bytes]]:
    """将 aiortc 解码帧标准化为无填充的 48kHz PCM16 mono。"""
    result: list[tuple[AudioFrame, bytes]] = []
    for output_frame in resampler.resample(frame):
        pcm = bytes(output_frame.planes[0])[: output_frame.samples * 2]
        if pcm:
            result.append((output_frame, pcm))
    return result


class RealtimeSession:
    """管理一个实时语音会话的完整生命周期。

    职责：
    - 建立 WebRTC 连接到 ChatGPT
    - 桥接客户端 WebSocket ↔ WebRTC 音频/事件
    """

    def __init__(
        self,
        identity: dict,
        model: str,
        websocket: WebSocket,
        access_token: str,
        access_token_provider: Callable[[set[str]], str] | None = None,
    ):
        self._identity = identity
        self._model = model
        self._ws = websocket
        self._access_token = access_token
        self._access_token_provider = access_token_provider
        self._pc = None
        self._input_track: BufferedAudioStreamTrack | None = None
        self._data_channel = None
        self._voice = "ember"
        self._closed = False
        self._tasks: list[asyncio.Task] = []
        self._dc_messages: asyncio.Queue[str] = asyncio.Queue()
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
        except RealtimeQuotaExceeded as e:
            if not self._closed:
                await self._send_error(str(e), code="realtime_quota_exhausted")
                logger.warning(f"[realtime] Voice quota exhausted: {e}")
        except Exception as e:
            if not self._closed:
                await self._send_error(str(e))
                logger.error(f"[realtime] Session error: {e}")
        finally:
            await self.close()

    async def _start(self) -> None:
        """建立 WebRTC 连接；额度耗尽时自动轮换到下一个账号。"""
        excluded: set[str] = set()
        access_token = self._access_token

        while True:
            try:
                await self._start_once(access_token)
                self._access_token = access_token
                return
            except RealtimeQuotaExceeded as exc:
                excluded.add(access_token)
                logger.warning("[realtime] Upstream account voice quota exhausted; trying next account")
                if self._pc:
                    await self._pc.close()
                self._pc = None
                self._input_track = None
                self._data_channel = None
                self._dc_messages = asyncio.Queue()
                if not self._access_token_provider:
                    raise
                try:
                    access_token = self._access_token_provider(excluded)
                except RuntimeError as provider_error:
                    raise RealtimeQuotaExceeded(str(provider_error)) from exc

    async def _start_once(self, access_token: str) -> None:
        """使用一个账号建立并探测 WebRTC 连接。"""
        self._pc, self._input_track, self._data_channel, remote_audio, self._location = await create_peer_connection(
            access_token=access_token,
            voice=self._voice,
        )
        self._remote_audio_track = remote_audio

        @self._data_channel.on("message")
        def on_dc_message(message):
            if isinstance(message, str):
                self._dc_messages.put_nowait(message)

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
            await self._check_initial_upstream_status()
        else:
            logger.warning(f"[realtime] DataChannel not open: {self._data_channel.readyState if self._data_channel else 'None'}")

    async def _check_initial_upstream_status(self) -> None:
        """捕获连接后立即下发的额度错误，同时保留普通消息供客户端读取。"""
        buffered: list[str] = []
        deadline = asyncio.get_running_loop().time() + 2.5
        try:
            while True:
                timeout = deadline - asyncio.get_running_loop().time()
                if timeout <= 0:
                    break
                try:
                    raw = await asyncio.wait_for(self._dc_messages.get(), timeout=timeout)
                except asyncio.TimeoutError:
                    break
                buffered.append(raw)
                try:
                    decoded = decode_data_channel_message(raw)
                except (json.JSONDecodeError, TypeError):
                    continue
                quota_error = quota_error_from_message(decoded)
                if quota_error:
                    raise RealtimeQuotaExceeded(quota_error)
                # 正常账号通常会立即给出不含限额警告的 usage_update。
                if decoded.get("type") == "usage_update":
                    break
        finally:
            for raw in buffered:
                self._dc_messages.put_nowait(raw)

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
            if self._input_track:
                self._input_track.clear()

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

        logger.info("[realtime] Audio sender started, remote track ready")
        # aiortc 的 OpusDecoder 固定输出 s16 stereo。浏览器调试面板消费的是
        # PCM16 mono，因此必须显式下混；直接读取 plane 会把 L/R 交错样本当
        # 成单声道，播放时长也会翻倍。
        resampler = AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
        recv_count = 0
        non_silence_count = 0
        silence_count = 0
        speaking = False
        next_send_at: float | None = None
        # 将 5 个 20ms 帧合并为约 100ms 的 WS 消息，降低 JSON/base64 和浏览器
        # AudioBufferSourceNode 的调度频率，也给网络抖动留出缓冲空间。
        output_buffer = bytearray()
        output_chunk_bytes = int(SAMPLE_RATE * 0.1) * 2

        async def flush_output(force: bool = False) -> None:
            if not output_buffer or (not force and len(output_buffer) < output_chunk_bytes):
                return
            audio_b64 = base64.b64encode(output_buffer).decode("ascii")
            output_buffer.clear()
            await self._send_event("response.audio.delta", {"delta": audio_b64})

        while not self._closed:
            try:
                frame = await asyncio.wait_for(track.recv(), timeout=5)
            except asyncio.TimeoutError:
                continue
            except Exception:
                break

            recv_count += 1
            for output_frame, pcm_bytes in resample_to_pcm16_mono(resampler, frame):

                samples_check = struct.unpack(f"<{len(pcm_bytes) // 2}h", pcm_bytes)
                rms = (sum(sample * sample for sample in samples_check) / len(samples_check)) ** 0.5
                is_silence = rms < 100

                if recv_count <= 3 or recv_count % 500 == 0:
                    logger.info(
                        f"[realtime] Remote audio frame #{recv_count}: "
                        f"source={frame.format.name}/{frame.layout.name}/{frame.sample_rate}Hz/{frame.samples}, "
                        f"output=s16/mono/{output_frame.sample_rate}Hz/{output_frame.samples}, "
                        f"rms={rms:.0f}, non_silence_total={non_silence_count}"
                    )

                if is_silence:
                    if not speaking:
                        continue
                    silence_count += 1
                else:
                    non_silence_count += 1
                    silence_count = 0
                    if not speaking:
                        speaking = True

                # 说话段中保留最多 500ms 静音，维持词句的正确时间关系；空闲
                # 静音则不推送，避免客户端永久累积播放队列。
                if silence_count > 25:
                    await flush_output(force=True)
                    speaking = False
                    silence_count = 0
                    next_send_at = None
                    await self._send_event("response.audio.done", {})
                    continue

                loop = asyncio.get_running_loop()
                now = loop.time()
                frame_duration = output_frame.samples / output_frame.sample_rate
                if next_send_at is None:
                    next_send_at = now
                else:
                    next_send_at = max(next_send_at + frame_duration, now)
                    await asyncio.sleep(max(0.0, next_send_at - now))

                output_buffer.extend(pcm_bytes)
                await flush_output()

        await flush_output(force=True)

    async def _dc_reader(self) -> None:
        """读取 DataChannel 消息并转发给客户端。"""
        if not self._data_channel:
            return

        while not self._closed:
            try:
                msg = await asyncio.wait_for(self._dc_messages.get(), timeout=2)
            except asyncio.TimeoutError:
                continue
            except Exception:
                break

            try:
                data = decode_data_channel_message(msg)
                event_type = data.get("type", "datachannel.message")
                payload = data.get("payload")
                if isinstance(payload, dict):
                    await self._send_event(event_type, payload)
                else:
                    await self._send_event(event_type, data)
            except (json.JSONDecodeError, TypeError):
                await self._send_event("datachannel.message", {"raw": msg[:1000]})

    async def _send_event(self, event_type: str, data: dict) -> None:
        if self._closed:
            return
        payload = {"type": event_type, **data}
        try:
            await self._ws.send_text(json.dumps(payload))
        except Exception:
            pass

    async def _send_error(self, message: str, code: str | None = None) -> None:
        error = {"message": message}
        if code:
            error["code"] = code
        await self._send_event("error", {"error": error})

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
