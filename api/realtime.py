from __future__ import annotations

import uuid

from fastapi import APIRouter, Header, HTTPException, Query, WebSocket, WebSocketDisconnect, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from api.support import require_identity
from services.account_service import account_service
from services.realtime.session import CHATGPT_WEB_REALTIME_MODEL, RealtimeSession
from services.realtime.signaling import (
    SignalingBusyError,
    UpstreamSignalingError,
    realtime_signaling_guard,
)
from services.realtime.chatgpt_webrtc import (
    REALTIME_DEFAULT_VOICE,
    REALTIME_VOICE_IDS,
    REALTIME_VOICES,
    exchange_realtime_sdp,
)
from utils.log import logger


class RealtimeOffer(BaseModel):
    sdp: str = Field(min_length=100, max_length=100_000)
    voice: str = Field(default=REALTIME_DEFAULT_VOICE, min_length=1, max_length=32)
    language: str = Field(default="auto", pattern=r"^(auto|[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8}){0,2})$")
    attempt_id: str | None = Field(default=None, min_length=16, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")

    @field_validator("voice")
    @classmethod
    def validate_voice(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in REALTIME_VOICE_IDS:
            raise ValueError(f"unsupported voice: {value}")
        return normalized


def _error_response(
    *,
    status_code: int,
    code: str,
    message: str,
    request_id: str,
    retryable: bool,
    retry_after: int | None = None,
) -> JSONResponse:
    error: dict[str, object] = {
        "code": code,
        "message": message,
        "retryable": retryable,
        "request_id": request_id,
    }
    headers = {"X-Request-ID": request_id}
    if retry_after is not None:
        error["retry_after_ms"] = retry_after * 1000
        headers["Retry-After"] = str(retry_after)
    return JSONResponse({"error": error}, status_code=status_code, headers=headers)


def create_router() -> APIRouter:
    router = APIRouter()

    @router.get("/v1/realtime/capabilities")
    async def get_realtime_capabilities(
        authorization: str | None = Header(default=None),
    ):
        """返回外部客户端接入实时语音所需的稳定协议能力。"""
        require_identity(authorization)
        return {
            "object": "realtime.capabilities",
            "protocol_version": 1,
            "transports": {
                "webrtc": {"signaling_url": "/v1/realtime/sessions", "recommended": True},
                "websocket": {
                    "url": "/v1/realtime",
                    "input_audio_format": "pcm16_mono_48000",
                    "output_audio_format": "pcm16_mono_48000",
                    "model": CHATGPT_WEB_REALTIME_MODEL,
                    "model_configurable": False,
                },
            },
            "default_voice": REALTIME_DEFAULT_VOICE,
            "voices_url": "/v1/realtime/voices",
            "authentication": "bearer",
            "event_envelope": "data_message",
            "max_input_audio_base64_chars": 512_000,
        }

    @router.get("/v1/realtime/voices")
    async def list_realtime_voices(
        authorization: str | None = Header(default=None),
    ):
        require_identity(authorization)
        return {
            "object": "list",
            "data": [
                {**voice, "object": "realtime.voice"}
                for voice in REALTIME_VOICES
            ],
        }

    @router.post("/v1/realtime/sessions")
    async def create_realtime_session(
        offer: RealtimeOffer,
        authorization: str | None = Header(default=None),
    ):
        """仅代理 SDP 信令；音频媒体在浏览器和 ChatGPT 之间直接走 WebRTC。"""
        identity = require_identity(authorization)
        identity_key = str(identity.get("id") or identity.get("name") or "anonymous")
        request_id = uuid.uuid4().hex
        retry_after = realtime_signaling_guard.check_rate_limit(identity_key)
        if retry_after:
            return _error_response(
                status_code=429,
                code="realtime_rate_limit_exceeded",
                message="Too many realtime signaling requests",
                request_id=request_id,
                retryable=True,
                retry_after=retry_after,
            )
        attempt_id, excluded = realtime_signaling_guard.open_attempt(identity_key, offer.attempt_id)
        try:
            access_token = account_service.get_realtime_access_token(excluded)
            realtime_signaling_guard.record_account(attempt_id, access_token)
            async with realtime_signaling_guard.signaling_slot():
                answer_sdp, location = await exchange_realtime_sdp(
                    access_token=access_token,
                    offer_sdp=offer.sdp,
                    voice=offer.voice,
                    language=offer.language,
                )
        except SignalingBusyError:
            return _error_response(
                status_code=503,
                code="realtime_signaling_busy",
                message="Realtime signaling is busy; retry shortly",
                request_id=request_id,
                retryable=True,
                retry_after=1,
            )
        except UpstreamSignalingError as exc:
            logger.warning(
                f"[realtime] Upstream signaling failed: request_id={request_id}, "
                f"status={exc.status_code}, detail_length={len(exc.detail)}"
            )
            status_code = 429 if exc.status_code == 429 else 502
            return _error_response(
                status_code=status_code,
                code="realtime_upstream_rate_limit" if status_code == 429 else "realtime_upstream_error",
                message="Upstream realtime service is temporarily unavailable",
                request_id=request_id,
                retryable=True,
                retry_after=2 if status_code == 429 else None,
            )
        except RuntimeError as exc:
            exhausted = "exhausted" in str(exc).lower()
            return _error_response(
                status_code=429 if exhausted else 503,
                code="realtime_quota_exhausted" if exhausted else "realtime_no_account",
                message=(
                    "All realtime-capable accounts have exhausted their voice quota"
                    if exhausted else "No realtime-capable account is currently available"
                ),
                request_id=request_id,
                retryable=not exhausted,
                retry_after=5 if not exhausted else None,
            )
        logger.info(
            f"[realtime] Direct WebRTC session: request_id={request_id}, voice={offer.voice}, "
            f"identity={identity.get('name')}"
        )
        return JSONResponse(
            {"sdp": answer_sdp, "location": location, "attempt_id": attempt_id, "request_id": request_id},
            headers={"X-Request-ID": request_id},
        )

    @router.post("/v1/realtime/sessions/{attempt_id}/quota-exhausted")
    async def report_realtime_quota_exhausted(
        attempt_id: str,
        authorization: str | None = Header(default=None),
    ):
        """将 DataChannel 观察到的语音额度耗尽反馈给信令账号选择器。"""
        identity = require_identity(authorization)
        identity_key = str(identity.get("id") or identity.get("name") or "anonymous")
        marked = realtime_signaling_guard.mark_quota_exhausted(identity_key, attempt_id)
        if not marked:
            return _error_response(
                status_code=404,
                code="realtime_attempt_not_found",
                message="Realtime attempt is unknown or expired",
                request_id=uuid.uuid4().hex,
                retryable=False,
            )
        logger.info(f"[realtime] Voice quota cooldown recorded: identity={identity.get('name')}")
        return {"ok": True, "cooldown_seconds": realtime_signaling_guard.quota_cooldown_seconds}

    @router.websocket("/v1/realtime")
    async def realtime_endpoint(
        websocket: WebSocket,
        model: str = Query(default=CHATGPT_WEB_REALTIME_MODEL),
        voice: str = Query(default=REALTIME_DEFAULT_VOICE),
    ):
        # 认证：从 header 或 query 中获取 token
        auth = (
            websocket.headers.get("authorization")
            or websocket.query_params.get("authorization")
            or (
                f"Bearer {websocket.query_params['api_key']}"
                if websocket.query_params.get("api_key")
                else None
            )
        )
        # 支持 OpenAI 的 subprotocol 方式传递 token
        if not auth:
            for proto in websocket.headers.get("sec-websocket-protocol", "").split(","):
                proto = proto.strip()
                if proto.startswith("openai-insecure-api-key."):
                    auth = f"Bearer {proto.removeprefix('openai-insecure-api-key.')}"
                    break

        try:
            identity = require_identity(auth)
        except HTTPException:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return

        voice = voice.strip().lower()
        if voice not in REALTIME_VOICE_IDS:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="unsupported voice")
            return

        # 获取 ChatGPT access_token
        try:
            access_token = account_service.get_realtime_access_token()
        except RuntimeError as e:
            await websocket.accept()
            import json
            await websocket.send_text(json.dumps({
                "type": "error",
                "error": {"message": str(e), "code": "no_account"}
            }))
            await websocket.close(code=1011)
            return

        await websocket.accept()
        logger.info(f"[realtime] New session: model={model}, identity={identity.get('name')}")

        session = RealtimeSession(
            identity=identity,
            model=model,
            websocket=websocket,
            access_token=access_token,
            access_token_provider=account_service.get_realtime_access_token,
            voice=voice,
        )
        try:
            await session.run()
        except WebSocketDisconnect:
            pass
        except Exception as e:
            logger.error(f"[realtime] Unhandled error: {e}")
        finally:
            await session.close()

    return router
