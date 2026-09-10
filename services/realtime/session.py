from __future__ import annotations

import array
import asyncio
import base64
import json
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from av import AudioFrame, AudioResampler
from fastapi import WebSocket

from services.realtime.audio_track import BufferedAudioStreamTrack, SAMPLE_RATE
from services.realtime.chatgpt_webrtc import create_peer_connection
from utils.log import logger

MAX_INPUT_AUDIO_B64_CHARS = 512_000
DATA_CHANNEL_QUEUE_SIZE = 512
AUDIO_OUT_QUEUE_SIZE = 80
EVENT_OUT_QUEUE_SIZE = 128
CHATGPT_WEB_REALTIME_MODEL = "chatgpt-web-voice"
OUTPUT_CHUNK_SECONDS = 0.04
MAX_TRAILING_SILENCE_SECONDS = 2.8
SILENCE_RMS_THRESHOLD = 100.0
END_AUDIO_STATES = frozenset({"listening", "idle"})
TRANSCRIPT_EVENT = "chat_message_delta"
GAP_FILL_THRESHOLD_SECONDS = 0.05
GAP_FILL_FALLBACK_SECONDS = 0.12
GAP_FILL_MAX_SECONDS = 1.2


class RealtimeQuotaExceeded(RuntimeError):
    """上游账号的实时语音额度已耗尽。"""


def decode_data_channel_message(message: str) -> dict[str, Any]:
    """解开 ChatGPT WebRTC 的 data_message 双层 JSON 封装。"""
    outer = json.loads(message)
    if not isinstance(outer, dict):
        raise TypeError("realtime data channel message must be a JSON object")
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


def data_channel_message_priority(message: str) -> int:
    """Rank messages so overload sheds telemetry before conversation state."""
    try:
        decoded = decode_data_channel_message(message)
    except (json.JSONDecodeError, TypeError):
        return 1
    event_type = str(decoded.get("type") or "")
    if event_type in {"error", "goodbye"} or event_type.endswith((".done", ".completed")):
        return 3
    if event_type == "chat_message_delta":
        return 2
    if event_type in {"state_update", "usage_update", "input_audio_buffer.speech_started", "input_audio_buffer.speech_stopped"}:
        return 0
    return 1


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


def media_time_seconds(frame: AudioFrame) -> float | None:
    """读取音频帧的媒体时钟；没有 pts 时返回 None。"""
    pts = getattr(frame, "pts", None)
    if pts is None:
        return None
    time_base = getattr(frame, "time_base", None)
    if time_base is not None:
        return float(pts * time_base)
    rate = getattr(frame, "sample_rate", None) or SAMPLE_RATE
    return pts / rate


def silence_to_cover_gap(
    *,
    speaking: bool,
    media_now: float | None,
    last_media: float | None,
    last_duration: float,
    waited: float,
) -> float:
    """只补媒体时间轴上的空洞，避免把事件循环卡顿误补成静音。"""
    if not speaking:
        return 0.0
    if media_now is not None and last_media is not None:
        extra = media_now - last_media - last_duration
        if extra > GAP_FILL_MAX_SECONDS + 0.5 or extra < GAP_FILL_THRESHOLD_SECONDS:
            return 0.0
        return min(extra, GAP_FILL_MAX_SECONDS)
    if waited >= GAP_FILL_FALLBACK_SECONDS:
        return min(max(0.0, waited - 0.04), GAP_FILL_MAX_SECONDS)
    return 0.0


def pcm16_rms(pcm: bytes) -> float:
    """计算 little-endian PCM16 的 RMS，供静音判断使用。"""
    length = len(pcm) - (len(pcm) % 2)
    if length < 2:
        return 0.0
    samples = array.array("h")
    samples.frombytes(pcm[:length])
    if sys.byteorder != "little":
        samples.byteswap()
    step = 4 if len(samples) >= 64 else 1
    total = 0
    count = 0
    for index in range(0, len(samples), step):
        sample = samples[index]
        total += sample * sample
        count += 1
    return (total / count) ** 0.5


@dataclass(frozen=True)
class AudioProtocolEvent:
    kind: Literal["delta", "done"]
    pcm: bytes = b""


class PcmOutputAssembler:
    """把上游 PCM 收成连续、等长的协议分片。

    WebRTC 收帧本身已经是实时节拍。这里只做聚合，丢弃开场静音，并把句中
    停顿原样送给客户端。不再按静音超时发送 audio.done——长播报里的换气/
    分段停顿不是一轮结束，提前 done 会让客户端整段重预填。
    """

    def __init__(
        self,
        chunk_bytes: int | None = None,
        max_silence_bytes: int | None = None,
        silence_rms: float = SILENCE_RMS_THRESHOLD,
    ):
        self.chunk_bytes = chunk_bytes if chunk_bytes is not None else int(SAMPLE_RATE * OUTPUT_CHUNK_SECONDS) * 2
        self.max_silence_bytes = (
            max_silence_bytes
            if max_silence_bytes is not None
            else int(SAMPLE_RATE * MAX_TRAILING_SILENCE_SECONDS) * 2
        )
        self.silence_rms = silence_rms
        self._buffer = bytearray()
        self._speaking = False
        self._silence_bytes = 0

    @property
    def speaking(self) -> bool:
        return self._speaking

    def push(self, pcm: bytes) -> list[AudioProtocolEvent]:
        if not pcm:
            return []
        is_silence = pcm16_rms(pcm) < self.silence_rms
        if is_silence and not self._speaking:
            return []

        if is_silence:
            self._silence_bytes += len(pcm)
        else:
            self._speaking = True
            self._silence_bytes = 0
        self._buffer.extend(pcm)
        return self._flush(force=False)

    def finish(self) -> list[AudioProtocolEvent]:
        events = self._flush(force=True)
        if self._speaking:
            events.append(AudioProtocolEvent("done"))
            self._speaking = False
            self._silence_bytes = 0
        return events

    def _flush(self, force: bool) -> list[AudioProtocolEvent]:
        events: list[AudioProtocolEvent] = []
        while len(self._buffer) >= self.chunk_bytes:
            chunk = bytes(self._buffer[: self.chunk_bytes])
            del self._buffer[: self.chunk_bytes]
            events.append(AudioProtocolEvent("delta", chunk))
        if force and self._buffer:
            events.append(AudioProtocolEvent("delta", bytes(self._buffer)))
            self._buffer.clear()
        return events


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
        account_available_callback: Callable[[str], object] | None = None,
        account_limited_callback: Callable[[str], object] | None = None,
        voice: str = "ember",
    ):
        self._identity = identity
        self._requested_model = model
        self._model = CHATGPT_WEB_REALTIME_MODEL
        self._ws = websocket
        self._access_token = access_token
        self._access_token_provider = access_token_provider
        self._account_available_callback = account_available_callback
        self._account_limited_callback = account_limited_callback
        self._pc = None
        self._input_track: BufferedAudioStreamTrack | None = None
        self._data_channel = None
        self._voice = voice
        self._closed = False
        self._tasks: list[asyncio.Task] = []
        self._dc_messages: asyncio.Queue[str] = asyncio.Queue(maxsize=DATA_CHANNEL_QUEUE_SIZE)
        self._dc_dropped_messages = 0
        self._audio_out: asyncio.Queue[str] = asyncio.Queue(maxsize=AUDIO_OUT_QUEUE_SIZE)
        self._event_out: asyncio.Queue[str] = asyncio.Queue(maxsize=EVENT_OUT_QUEUE_SIZE)
        self._out_ready = asyncio.Event()
        self._writer_started = False
        self._output_assembler = PcmOutputAssembler()
        self._end_audio_output = False
        self._latest_transcript: str | None = None
        self._last_media_s: float | None = None
        self._last_frame_s = 0.0
        self._start_time = time.time()

    async def run(self) -> None:
        """主运行循环 — 建立连接后并发处理 WS 读/写。"""
        writer_task = asyncio.create_task(self._ws_writer(), name="ws-writer")
        self._tasks = [writer_task]
        self._writer_started = True
        try:
            await self._start()
            await self._send_event("session.created", {
                "session": {"id": self._location, "model": self._model, "voice": self._voice}
            })
            if self._requested_model and self._requested_model != self._model:
                await self._send_event("warning", {
                    "warning": {
                        "code": "model_not_configurable",
                        "message": f"ChatGPT Web Voice uses {self._model}; requested model was ignored",
                    }
                })

            reader_task = asyncio.create_task(self._client_reader(), name="ws-reader")
            sender_task = asyncio.create_task(self._audio_sender(), name="audio-sender")
            dc_task = asyncio.create_task(self._dc_reader(), name="dc-reader")
            self._tasks = [writer_task, reader_task, sender_task, dc_task]

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
                if self._account_available_callback:
                    self._account_available_callback(access_token)
                return
            except RealtimeQuotaExceeded as exc:
                if self._account_limited_callback:
                    self._account_limited_callback(access_token)
                excluded.add(access_token)
                logger.warning("[realtime] Upstream account voice quota exhausted; trying next account")
                if self._pc:
                    await self._pc.close()
                self._pc = None
                self._input_track = None
                self._data_channel = None
                self._dc_messages = asyncio.Queue(maxsize=DATA_CHANNEL_QUEUE_SIZE)
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
                self._queue_dc_message(message)

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
                self._queue_dc_message(raw)

    def _queue_dc_message(self, message: str) -> None:
        if self._dc_messages.full():
            incoming_priority = data_channel_message_priority(message)
            queued = self._dc_messages._queue  # asyncio.Queue intentionally exposes its backing deque internally.
            drop_index = next(
                (index for index, queued_message in enumerate(queued)
                 if data_channel_message_priority(queued_message) < incoming_priority),
                None,
            )
            if drop_index is None:
                self._dc_dropped_messages += 1
                return
            del queued[drop_index]
            self._dc_dropped_messages += 1
        try:
            self._dc_messages.put_nowait(message)
        except asyncio.QueueFull:
            self._dc_dropped_messages += 1

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
                    if not isinstance(audio_b64, str) or len(audio_b64) > MAX_INPUT_AUDIO_B64_CHARS:
                        raise ValueError("audio chunk is too large")
                    pcm_data = base64.b64decode(audio_b64, validate=True)
                    self._input_track.push_pcm16(pcm_data)
                    if not hasattr(self, "_audio_log_count"):
                        self._audio_log_count = 0
                    self._audio_log_count += 1
                    if self._audio_log_count <= 3 or self._audio_log_count % 100 == 0:
                        logger.info(
                            f"[realtime] Audio input: chunk={self._audio_log_count}, "
                            f"bytes={len(pcm_data)}, queue={self._input_track.buffered_frames}, "
                            f"dropped={self._input_track.dropped_frames}"
                        )
                except Exception as e:
                    logger.error(f"[realtime] Audio decode error: {e}")
                    await self._send_error(str(e), code="invalid_audio")

        elif event_type == "input_audio_buffer.commit":
            pass  # VAD 由 ChatGPT 服务端处理

        elif event_type == "input_audio_buffer.clear":
            if self._input_track:
                self._input_track.clear()

        elif event_type == "session.update":
            session = event.get("session", {})
            requested_voice = session.get("voice")
            if requested_voice and requested_voice != self._voice:
                await self._send_error(
                    "voice must be selected before the WebSocket session starts",
                    code="unsupported_session_update",
                )
            else:
                await self._send_event("session.updated", {"session": {"voice": self._voice}})

        elif event_type == "response.cancel":
            self._send_to_dc({"type": "response.cancel"})

        elif event_type in ("relay_message", "conversation.item.create", "response.create",
                            "conversation.item.delete", "conversation.item.truncate"):
            self._send_to_dc(event)
        else:
            await self._send_error(f"unsupported realtime event: {event_type or '<empty>'}", code="unsupported_event")

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
        assembler = self._output_assembler
        recv_count = 0

        async def emit(events: list[AudioProtocolEvent]) -> None:
            for event in events:
                if event.kind == "delta":
                    audio_b64 = base64.b64encode(event.pcm).decode("ascii")
                    await self._send_event("response.audio.delta", {"delta": audio_b64})
                else:
                    await self._send_event("response.audio.done", {})

        while not self._closed:
            if self._end_audio_output:
                self._end_audio_output = False
                await emit(assembler.finish())
                self._last_media_s = None
                self._last_frame_s = 0.0
            waited_at = time.monotonic()
            try:
                frame = await asyncio.wait_for(track.recv(), timeout=5)
            except asyncio.TimeoutError:
                continue
            except Exception as exc:
                if not self._closed:
                    logger.warning(f"[realtime] Remote audio track ended: {exc}")
                break

            # 只按媒体时间轴补 DTX 空洞。事件循环卡住时 pts 不会跳，
            # 此时再按墙钟补静音会把播放缓冲垫高，客户端听起来越来越快。
            waited = time.monotonic() - waited_at
            media_now = media_time_seconds(frame)
            pad_s = silence_to_cover_gap(
                speaking=assembler.speaking,
                media_now=media_now,
                last_media=self._last_media_s,
                last_duration=self._last_frame_s,
                waited=waited,
            )
            if pad_s >= 0.02:
                await emit(assembler.push(b"\x00\x00" * int(SAMPLE_RATE * pad_s)))
            if media_now is not None:
                self._last_media_s = media_now
            self._last_frame_s = (frame.samples / frame.sample_rate) if frame.sample_rate else 0.0

            recv_count += 1
            for output_frame, pcm_bytes in resample_to_pcm16_mono(resampler, frame):
                if recv_count <= 3 or recv_count % 500 == 0:
                    logger.info(
                        f"[realtime] Remote audio frame #{recv_count}: "
                        f"source={frame.format.name}/{frame.layout.name}/{frame.sample_rate}Hz/{frame.samples}, "
                        f"output=s16/mono/{output_frame.sample_rate}Hz/{output_frame.samples}, "
                        f"rms={pcm16_rms(pcm_bytes):.0f}"
                    )
                await emit(assembler.push(pcm_bytes))

        await emit(assembler.finish())

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
                    new_state = payload.get("new_state")
                else:
                    await self._send_event(event_type, data)
                    new_state = data.get("new_state")
                if event_type == "state_update" and new_state in END_AUDIO_STATES:
                    self._end_audio_output = True
                quota_error = quota_error_from_message(data)
                if quota_error:
                    if self._account_limited_callback:
                        self._account_limited_callback(self._access_token)
                    await self._send_error(quota_error, code="realtime_quota_exhausted")
                    return
            except (json.JSONDecodeError, TypeError):
                await self._send_event("datachannel.message", {"raw": msg[:1000]})

    def _outbound_idle(self) -> bool:
        return self._audio_out.empty() and self._latest_transcript is None and self._event_out.empty()

    async def _ws_writer(self) -> None:
        """单写者：音频优先；转写只保留最新一帧，避免把播报卡出缺口。"""
        while not self._closed:
            if self._outbound_idle():
                self._out_ready.clear()
                if self._outbound_idle():
                    try:
                        await asyncio.wait_for(self._out_ready.wait(), timeout=2)
                    except asyncio.TimeoutError:
                        continue
                continue
            try:
                if not self._audio_out.empty():
                    payload = self._audio_out.get_nowait()
                elif self._latest_transcript is not None:
                    payload = self._latest_transcript
                    self._latest_transcript = None
                else:
                    payload = self._event_out.get_nowait()
            except asyncio.QueueEmpty:
                continue
            try:
                await self._ws.send_text(payload)
            except Exception as exc:
                if not self._closed:
                    logger.debug(f"[realtime] WebSocket send stopped: {exc}")
                return

    async def _send_event(self, event_type: str, data: dict) -> None:
        if self._closed:
            return
        payload = json.dumps({"type": event_type, **data})
        try:
            if not self._writer_started:
                await self._ws.send_text(payload)
                return
            if event_type.startswith("response.audio."):
                await self._audio_out.put(payload)
            elif event_type == TRANSCRIPT_EVENT:
                self._latest_transcript = payload
            else:
                if self._event_out.full():
                    try:
                        self._event_out.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                try:
                    self._event_out.put_nowait(payload)
                except asyncio.QueueFull:
                    return
            self._out_ready.set()
        except Exception as exc:
            if not self._closed:
                logger.debug(f"[realtime] WebSocket send stopped: {exc}")

    async def _send_error(self, message: str, code: str | None = None) -> None:
        error = {"message": message}
        if code:
            error["code"] = code
        await self._send_event("error", {"error": error})

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._out_ready.set()

        pending_tasks = [task for task in self._tasks if not task.done()]
        for task in pending_tasks:
            task.cancel()
        if pending_tasks:
            await asyncio.gather(*pending_tasks, return_exceptions=True)

        if self._pc:
            try:
                await self._pc.close()
            except Exception:
                pass

        duration = time.time() - self._start_time
        logger.info(
            f"[realtime] Session closed after {duration:.1f}s "
            f"(dc_dropped={self._dc_dropped_messages})"
        )
