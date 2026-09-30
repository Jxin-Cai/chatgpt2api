from __future__ import annotations

import asyncio
import base64
import binascii
import json
import time
import uuid

from services.live.protocol import (
    LiveConfig, LiveError, MAX_MESSAGE_BYTES, PcmConverter, SESSION_SECONDS,
    TranscriptAdapter, check_text, error_event, event, object_value, only_fields,
)
from services.realtime.session import RealtimeSession, RealtimeQuotaExceeded


class LiveSession(RealtimeSession):
    """Own the Live lifecycle while reusing only the private upstream media engine."""
    def __init__(self, *, config: LiveConfig, session_id: str | None = None, **kwargs):
        super().__init__(model="chatgpt-web-voice", voice=config.upstream_voice, **kwargs)
        self.config = config
        self.id = session_id or f"live_{uuid.uuid4().hex}"
        self.expires_at = int(time.time()) + SESSION_SECONDS
        self._clock: float | None = None
        self._stop = asyncio.Event()
        self._prepared = False
        self._media_task: asyncio.Task | None = None
        self._muted = False
        self._reason = "connection_lost"
        self._close_event_id: str | None = None
        self._start_event_id: str | None = None
        self._input_converter = PcmConverter(config.rate, 48000)
        self._output_converter = PcmConverter(48000, config.rate)
        self._audio_out = asyncio.Queue(maxsize=8)  # at most ~320ms pending PCM
        self._context_message_ids: set[str] = set()
        self._transcript = TranscriptAdapter(self._context_message_ids)

    @property
    def snapshot(self) -> dict:
        return self.config.snapshot(self.id, self.expires_at)

    def milliseconds(self) -> int:
        return max(0, int((time.monotonic() - (self._clock or time.monotonic())) * 1000))

    async def prepare(self) -> None:
        if self._prepared:
            return
        await asyncio.wait_for(self._start(), timeout=45)
        if self.config.transport == "webrtc":
            # Drain upstream RTP while the browser completes ICE/DTLS. Otherwise
            # seconds of decoded startup frames burst into the small output queue.
            self._media_task = asyncio.create_task(self._audio_sender(), name="live-audio")
            self._tasks.append(self._media_task)
        self._prepared = True

    async def serve(self, start_event_id: str | None = None) -> None:
        self._start_event_id = start_event_id
        started = False
        try:
            await self.prepare()
            wait_ready = getattr(self._ws, "wait_ready", None)
            if wait_ready:
                await wait_ready()
            self._clock = time.monotonic()
            self.expires_at = int(time.time()) + SESSION_SECONDS
            context = self.config.initial_context()
            if context:
                self._relay(context)
            fields = {"session": self.snapshot}
            if start_event_id:
                fields["client_event_id"] = start_event_id
            await self._ws.send_text(json.dumps(event("session.started", **fields)))
            started = True
            self._writer_started = True
            self._tasks = [
                asyncio.create_task(self._ws_writer(), name="live-writer"),
                asyncio.create_task(self._client_reader(), name="live-reader"),
                self._media_task or asyncio.create_task(self._audio_sender(), name="live-audio"),
                asyncio.create_task(self._dc_reader(), name="live-events"),
                asyncio.create_task(self._stop.wait(), name="live-stop"),
                asyncio.create_task(asyncio.sleep(SESSION_SECONDS), name="live-expiry"),
            ]
            done, _ = await asyncio.wait(self._tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                if task.get_name() == "live-expiry":
                    self._reason = "expired"
                if not task.cancelled() and task.exception():
                    raise task.exception()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Upstream exceptions may include provider internals; expose only a stable code.
            code = "quota_exhausted" if isinstance(exc, RealtimeQuotaExceeded) else "upstream_unavailable"
            try:
                # Stop the single writer before sending a terminal error.
                await super().close()
                await asyncio.wait_for(self._ws.send_text(json.dumps(error_event(
                    LiveError("Voice connection ended before it could complete", code=code), start_event_id if not started else None,
                ))), timeout=2)
            except Exception:
                pass
        finally:
            seconds = self.milliseconds() / 1000
            # Stop producers, then allow already accepted control/transcript events
            # to drain before the terminal event. Never replay them on a new socket.
            producers = [task for task in self._tasks if task.get_name() != "live-writer"]
            for task in producers:
                task.cancel()
            await asyncio.gather(*producers, return_exceptions=True)
            while not self._audio_out.empty():
                self._audio_out.get_nowait()
            if started and not self._closed:
                deadline = asyncio.get_running_loop().time() + 1
                while (not self._outbound_idle() or self._outbound_sending) and asyncio.get_running_loop().time() < deadline:
                    await asyncio.sleep(.01)
            await super().close()
            if started:
                fields = {"session": self.snapshot, "reason": self._reason, "usage": {"seconds": seconds}}
                if self._close_event_id:
                    fields["client_event_id"] = self._close_event_id
                try:
                    await asyncio.wait_for(self._ws.send_text(json.dumps(event("session.closed", **fields))), timeout=2)
                except Exception:
                    pass  # A lost transport cannot confirm finalization.
            close_transport = getattr(self._ws, "close", None)
            if close_transport:
                try:
                    await close_transport()
                except Exception:
                    pass  # The peer may already have closed the transport.

    def request_close(self, client_event_id: str | None = None) -> None:
        self._reason = "close_requested"
        self._close_event_id = client_event_id
        self._stop.set()

    def feed_media(self, pcm: bytes) -> None:
        if self._clock is not None and not self._muted and not self._closed and self._input_track:
            self._input_track.push_pcm16(pcm)

    def _relay(self, text: str) -> str:
        if not self._data_channel or self._data_channel.readyState != "open":
            raise LiveError("Upstream event channel is unavailable", code="upstream_unavailable")
        message_id = str(uuid.uuid4())
        self._context_message_ids.add(message_id)
        self._send_to_dc({
            "type": "relay_message", "payload": {"type": "relay_message", "message": {
                "id": message_id, "author": {"role": "user"}, "create_time": time.time(),
                "content": {"content_type": "text", "parts": [text]},
                "metadata": {"serialization_metadata": {"custom_symbol_offsets": []}},
                "clientMetadata": {"isOptimistic": True},
            }},
        })
        return message_id

    async def _client_reader(self) -> None:
        while not self._closed and not self._stop.is_set():
            try:
                raw = await self._ws.receive_text()
            except Exception:
                return
            client_id = None
            try:
                if len(raw.encode("utf-8")) > MAX_MESSAGE_BYTES:
                    raise LiveError("Event exceeds the local size limit", code="event_too_large")
                data = object_value(json.loads(raw), "event")
                client_id = data.get("event_id")
                if client_id is not None and (not isinstance(client_id, str) or len(client_id) > 256):
                    client_id = None
                    raise LiveError("event_id must be a string of at most 256 characters", "event_id")
                await self._handle_live_event(data)
            except (json.JSONDecodeError, UnicodeError):
                await self._emit(error_event(LiveError("Invalid JSON", code="invalid_json")))
            except LiveError as exc:
                await self._emit(error_event(exc, client_id))

    async def _handle_live_event(self, data: dict) -> None:
        kind = data.get("type")
        if not isinstance(kind, str):
            raise LiveError("type must be an event name", "type")
        client_id = data.get("event_id")
        base_fields = {"type", "event_id"}
        if kind == "session.input_audio.append":
            only_fields(data, base_fields | {"audio"}, "")
            if self.config.transport != "websocket":
                raise LiveError("WebRTC audio uses media tracks", "type")
            encoded = data.get("audio")
            if not isinstance(encoded, str):
                raise LiveError("audio must be base64 PCM16", "audio")
            try:
                pcm = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error):
                raise LiveError("audio must be valid base64", "audio")
            if len(pcm) % 2:
                raise LiveError("PCM16 requires complete samples", "audio")
            # Live callers must pace audio. Never silently truncate an oversized burst.
            if len(pcm) > self.config.rate * 2 // 4:
                raise LiveError("Send paced audio chunks of at most 250 ms", "audio", "audio_chunk_too_large")
            if not self._muted and self._input_track:
                self._input_track.push_pcm16(self._input_converter.push(pcm))
        elif kind in {"session.input_audio.mute", "session.input_audio.unmute"}:
            only_fields(data, base_fields, "")
            self._muted = kind.endswith(".mute")
            if self._input_track:
                self._input_track.clear()
            self._input_converter = PcmConverter(self.config.rate, 48000)
            self._send_to_dc({"type": "track_state", "payload": {
                "type": "track_state", "track_id": "microphone", "media_type": "audio",
                "media_source": "microphone", "state": "muted" if self._muted else "live",
            }})
            await self._emit(event("session.input_audio.muted" if self._muted else "session.input_audio.unmuted", **({"client_event_id": client_id} if client_id else {})))
        elif kind in {"session.instructions.append", "session.commentary.append"}:
            only_fields(data, base_fields | {"content", "delegation_id"}, "")
            if "delegation_id" not in data or data["delegation_id"] is not None:
                raise LiveError("This adapter requires delegation_id: null", "delegation_id", "unsupported_delegation")
            content = check_text(data.get("content"), "content", 500)
            if not content.strip():
                raise LiveError("content must not be empty", "content")
            prefix = "Follow this conversation instruction: " if kind == "session.instructions.append" else "Communicate this context to the user naturally: "
            self._relay(prefix + content)
            now = self.milliseconds()
            await self._emit(event(kind + "ed", start_ms=now, end_ms=now, **({"client_event_id": client_id} if client_id else {})))
        elif kind == "session.update":
            only_fields(data, base_fields | {"session"}, "")
            update = object_value(data.get("session"), "session")
            if update:
                key = next(iter(update))
                immutable = key in {"model", "audio", "instructions", "input", "client"}
                raise LiveError("This session setting cannot be updated by the adapter", f"session.{key}", "immutable_field_update" if immutable else "unsupported_parameter")
            await self._emit(event("session.updated", session=self.snapshot, **({"client_event_id": client_id} if client_id else {})))
        elif kind == "session.close":
            only_fields(data, base_fields, "")
            self.request_close(client_id)
        else:
            raise LiveError(f"Unsupported Live command: {kind}", "type", "unsupported_event")

    async def _emit(self, payload: dict) -> None:
        await super()._send_event(payload["type"], payload)

    async def _send_event(self, event_type: str, data: dict) -> None:
        # Whitelist conversion: private Web Voice and legacy Realtime events never leak.
        if event_type == "response.audio.delta":
            if self._output_suppressed:
                return
            pcm = self._output_converter.push(base64.b64decode(data["delta"]))
            if pcm:
                await self._emit(event("session.output_audio.delta", delta=base64.b64encode(pcm).decode("ascii")))
        elif event_type == "chat_message_delta":
            try:
                fragment = self._transcript.push(data)
                if fragment:
                    role, text = fragment
                    end = self.milliseconds()
                    # The private provider exposes no aligned timestamps. Use an
                    # honest local arrival point rather than inventing word timing.
                    start = end
                    await self._emit(event(
                        "session.input_transcript.delta" if role == "user" else "session.output_transcript.delta",
                        delta=text, start_ms=start, end_ms=end,
                    ))
            except LiveError as exc:
                await self._emit(error_event(exc))
        elif event_type == "input_audio_buffer.speech_started":
            self._interrupt_output()
        elif event_type == "state_update":
            if data.get("new_state") == "speaking":
                self._output_suppressed = False
        elif event_type == "goodbye":
            self._reason = "remote_hangup" if data.get("reason") != "cap_reached" else "connection_lost"
            self._stop.set()
        elif event_type == "error":
            await self._emit(error_event(LiveError("Upstream voice service reported an error", code=(data.get("error") or {}).get("code", "upstream_error"))))

    def _interrupt_output(self) -> None:
        self._output_suppressed = True
        while not self._audio_out.empty():
            self._audio_out.get_nowait()
        self._output_assembler.discard()
        self._output_converter = PcmConverter(48000, self.config.rate)
        clear = getattr(self._ws, "clear_audio", None)
        if clear:
            clear()
