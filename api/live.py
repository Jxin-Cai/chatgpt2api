from __future__ import annotations

import asyncio
import json
import os
import uuid

from fastapi import APIRouter, Header, HTTPException, Request, WebSocket
from fastapi.responses import JSONResponse

from api.support import require_identity
from services.account_service import account_service
from services.live.protocol import (
    LiveError, MAX_MESSAGE_BYTES, MODEL, error_event, object_value, only_fields, parse_config,
)
from services.live.session import LiveSession
from services.live.transport import WebRTCTransport
from services.realtime.chatgpt_webrtc import OFFICIAL_VOICE_ALIASES
from services.realtime.signaling import SignalingBusyError, realtime_signaling_guard
from utils.log import logger


class LiveRuntime:
    def __init__(self):
        self.sessions: dict[str, tuple[str, LiveSession | None]] = {}
        self.tasks: set[asyncio.Task] = set()

    def reserve(self, identity: dict) -> str:
        owner = str(identity.get("id") or identity.get("name") or "anonymous")
        retry = realtime_signaling_guard.check_rate_limit(owner)
        if retry:
            raise LiveError("Too many session creation requests", code="rate_limit_exceeded")
        total_limit = max(1, int(os.getenv("CHATGPT2API_LIVE_MAX_SESSIONS", "32")))
        per_user = max(1, int(os.getenv("CHATGPT2API_LIVE_MAX_SESSIONS_PER_USER", "4")))
        if len(self.sessions) >= total_limit or sum(key == owner for key, _ in self.sessions.values()) >= per_user:
            raise LiveError("Concurrent voice session limit reached", code="rate_limit_exceeded")
        session_id = "live_" + uuid.uuid4().hex
        self.sessions[session_id] = (owner, None)
        return session_id

    def bind(self, session: LiveSession) -> None:
        owner, _ = self.sessions[session.id]
        self.sessions[session.id] = (owner, session)

    async def shutdown(self) -> None:
        for _, session in list(self.sessions.values()):
            if session:
                session.request_close()
        if self.tasks:
            done, pending = await asyncio.wait(self.tasks, timeout=5)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)


live_runtime = LiveRuntime()


def make_session(identity: dict, config, transport, session_id: str) -> LiveSession:
    token = account_service.get_realtime_access_token()
    return LiveSession(
        identity=identity, websocket=transport, access_token=token, config=config, session_id=session_id,
        access_token_provider=account_service.get_realtime_access_token,
        account_available_callback=account_service.mark_realtime_available,
        account_limited_callback=lambda token: account_service.mark_realtime_unavailable(
            token, status="limited", reason="quota_exhausted",
            cooldown_seconds=realtime_signaling_guard.quota_cooldown_seconds,
        ),
    )


def http_error(exc: LiveError, status: int = 400) -> JSONResponse:
    if exc.code == "rate_limit_exceeded":
        status = 429
    detail = error_event(exc)["error"]
    return JSONResponse({"error": detail}, status_code=status)


async def bounded_json(request: Request) -> dict:
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_MESSAGE_BYTES:
            raise LiveError("Request is too large", code="request_too_large")
    try:
        return object_value(json.loads(body), "request")
    except (json.JSONDecodeError, UnicodeError):
        raise LiveError("Invalid JSON", code="invalid_json")


def create_router() -> APIRouter:
    router = APIRouter()

    @router.get("/v1/live/capabilities")
    async def capabilities(authorization: str | None = Header(default=None)):
        require_identity(authorization)
        return {
            "object": "live.capabilities", "model": MODEL, "upstream": "chatgpt-web-voice",
            "compatibility": "voice-session subset", "transports": ["webrtc", "websocket"],
            "sessions_url": "/v1/live/sessions", "voices": OFFICIAL_VOICE_ALIASES,
            "pcm_sample_rates": [16000, 24000], "custom_voices": False,
            "delegation": False, "sideband": False, "store": False, "fork": False,
            "instructions": "text-context approximation", "input": "text-context approximation",
            "transcript_timestamps": "local arrival-time approximation",
            "usage": "local elapsed seconds; not upstream billing",
            "max_audio_chunk_ms": 250,
        }

    @router.post("/v1/live/sessions")
    async def create_session(request: Request, authorization: str | None = Header(default=None)):
        identity = require_identity(authorization)
        transport = None
        session = None
        session_id = None
        try:
            body = await bounded_json(request)
            only_fields(body, {"session", "transport"}, "")
            config = parse_config(body.get("session"), "webrtc")
            spec = object_value(body.get("transport"), "transport")
            only_fields(spec, {"type", "sdp"}, "transport")
            if spec.get("type") != "webrtc" or not isinstance(spec.get("sdp"), str) or not 100 <= len(spec["sdp"]) <= 100_000:
                raise LiveError("Expected a WebRTC SDP offer", "transport.sdp")
            session_id = live_runtime.reserve(identity)
            transport = WebRTCTransport()
            # Validate browser SDP before consuming an upstream session.
            answer = await asyncio.wait_for(transport.answer(spec["sdp"]), timeout=15)
            session = await asyncio.to_thread(make_session, identity, config, transport, session_id)
            transport.on_audio = session.feed_media
            transport.on_disconnect = session._stop.set
            live_runtime.bind(session)
            async with realtime_signaling_guard.signaling_slot():
                await session.prepare()

            async def serve():
                try:
                    await session.serve()
                finally:
                    live_runtime.sessions.pop(session.id, None)
            task = asyncio.create_task(serve(), name=f"live-{session.id}")
            live_runtime.tasks.add(task)
            task.add_done_callback(live_runtime.tasks.discard)
            return JSONResponse({"session": {"id": session.id}, "transport": {"type": "webrtc", "sdp": answer}}, status_code=201)
        except LiveError as exc:
            response = http_error(exc)
        except (ValueError, AssertionError):
            response = http_error(LiveError("Invalid WebRTC SDP or media configuration", "transport.sdp"))
        except (SignalingBusyError, asyncio.TimeoutError, RuntimeError):
            response = http_error(LiveError("Voice session could not be established", code="upstream_unavailable"), 503)
        except asyncio.CancelledError:
            if session:
                await session.close()
            if transport:
                await transport.close()
            if session_id:
                live_runtime.sessions.pop(session_id, None)
            raise
        except Exception:
            response = http_error(LiveError("Voice session could not be established", code="upstream_unavailable"), 502)
        if session:
            await session.close()
        if transport:
            await transport.close()
        if session_id:
            live_runtime.sessions.pop(session_id, None)
        return response

    @router.websocket("/v1/live/sessions")
    async def websocket_session(websocket: WebSocket):
        # Header auth is the official server-to-server path. Browser subprotocol
        # auth is retained solely for this app's same-origin fallback.
        auth = websocket.headers.get("authorization")
        subprotocol = None
        if not auth:
            for value in websocket.headers.get("sec-websocket-protocol", "").split(","):
                value = value.strip()
                if value.startswith("openai-insecure-api-key."):
                    auth = "Bearer " + value.removeprefix("openai-insecure-api-key.")
                    subprotocol = value
                    break
        try:
            identity = require_identity(auth)
        except HTTPException:
            await websocket.close(code=1008)
            return
        await websocket.accept(subprotocol=subprotocol)
        session_id = None
        client_id = None
        task = asyncio.current_task()
        if task:
            live_runtime.tasks.add(task)
        try:
            if websocket.query_params:
                raise LiveError("Put model and configuration in session.start, not URL query parameters", code="unknown_parameter")
            raw = await asyncio.wait_for(websocket.receive_text(), timeout=10)
            if len(raw.encode("utf-8")) > MAX_MESSAGE_BYTES:
                raise LiveError("Event is too large", code="event_too_large")
            data = object_value(json.loads(raw), "event")
            client_id = data.get("event_id")
            if client_id is not None and (not isinstance(client_id, str) or len(client_id) > 256):
                client_id = None
                raise LiveError("Invalid event_id", "event_id")
            only_fields(data, {"type", "event_id", "session"}, "")
            if data.get("type") != "session.start":
                raise LiveError("First command must be session.start", "type", "session_not_started")
            config = parse_config(data.get("session"))
            session_id = live_runtime.reserve(identity)
            session = await asyncio.to_thread(make_session, identity, config, websocket, session_id)
            live_runtime.bind(session)
            async with realtime_signaling_guard.signaling_slot():
                await session.prepare()
            await session.serve(client_id)
            return
        except (json.JSONDecodeError, UnicodeError):
            exc = LiveError("Invalid JSON", code="invalid_json")
        except LiveError as error:
            exc = error
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning(f"[live] WebSocket startup failed: {type(error).__name__}")
            exc = LiveError("Voice session could not be established", code="upstream_unavailable")
        finally:
            if task:
                live_runtime.tasks.discard(task)
            if session_id:
                entry = live_runtime.sessions.pop(session_id, None)
                if entry and entry[1]:
                    await entry[1].close()
        try:
            await websocket.send_json(error_event(exc, client_id))
            await websocket.close(code=1008)
        except Exception:
            pass

    @router.api_route("/v1/live/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def unsupported(path: str, authorization: str | None = Header(default=None)):
        require_identity(authorization)
        return http_error(LiveError("This Live resource is not supported by the voice adapter", code="unsupported_endpoint"), 501)

    return router
