from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Header, HTTPException, Query, WebSocket, WebSocketDisconnect, status
from fastapi.responses import JSONResponse, StreamingResponse
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


class RealtimeTextInput(BaseModel):
    text: str = Field(min_length=1, max_length=10_000)
    conversation_id: str = Field(min_length=10, max_length=100)
    parent_message_id: str = Field(default="", max_length=100)


class RealtimeQuotaReport(BaseModel):
    reason: str = Field(default="quota_exhausted", max_length=120)
    restore_at: datetime | None = None
    retry_after_seconds: int | None = Field(default=None, ge=1, le=7 * 24 * 60 * 60)

    @field_validator("restore_at")
    @classmethod
    def normalize_restore_at(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


def _error_response(
    *,
    status_code: int,
    code: str,
    message: str,
    request_id: str,
    retryable: bool,
    retry_after: int | None = None,
    attempt_id: str | None = None,
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
    payload: dict[str, object] = {"error": error}
    if attempt_id:
        payload["attempt_id"] = attempt_id
    return JSONResponse(payload, status_code=status_code, headers=headers)


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
        access_token = ""
        try:
            access_token = account_service.get_realtime_access_token(excluded)
            async with realtime_signaling_guard.signaling_slot():
                realtime_signaling_guard.record_account(attempt_id, access_token)
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
                attempt_id=attempt_id,
            )
        except UpstreamSignalingError as exc:
            logger.warning(
                f"[realtime] Upstream signaling failed: request_id={request_id}, "
                f"status={exc.status_code}, detail_length={len(exc.detail)}"
            )
            account_limited = exc.is_quota_limited
            account_unavailable = exc.status_code in {401, 403} and not account_limited
            cooldown_seconds = (
                exc.retry_after_seconds
                or (realtime_signaling_guard.quota_cooldown_seconds if account_limited else 300)
            )
            if access_token and (account_limited or account_unavailable):
                realtime_signaling_guard.cool_account(access_token, cooldown_seconds)
                account_service.mark_realtime_unavailable(
                    access_token,
                    status="limited" if account_limited else "unavailable",
                    reason=(
                        "upstream_voice_quota"
                        if account_limited
                        else f"upstream_http_{exc.status_code}"
                    ),
                    cooldown_seconds=cooldown_seconds,
                )
            status_code = 429 if account_limited else 502
            return _error_response(
                status_code=status_code,
                code="realtime_voice_quota_limited" if account_limited else "realtime_upstream_error",
                message=(
                    "Selected account has exhausted its realtime voice quota"
                    if account_limited
                    else "Upstream realtime service is temporarily unavailable"
                ),
                request_id=request_id,
                retryable=True,
                retry_after=min(cooldown_seconds, 5) if account_limited else None,
                attempt_id=attempt_id,
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
                attempt_id=attempt_id,
            )
        account_service.mark_realtime_available(access_token)
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
        report: RealtimeQuotaReport | None = None,
        authorization: str | None = Header(default=None),
    ):
        """将 DataChannel 观察到的语音额度耗尽反馈给信令账号选择器。"""
        identity = require_identity(authorization)
        identity_key = str(identity.get("id") or identity.get("name") or "anonymous")
        restore_at = report.restore_at.isoformat() if report and report.restore_at else None
        restore_delay = (
            max(1, int((report.restore_at - datetime.now(timezone.utc)).total_seconds()))
            if report and report.restore_at and report.restore_at > datetime.now(timezone.utc)
            else None
        )
        cooldown_seconds = (
            restore_delay
            or (report.retry_after_seconds if report and report.retry_after_seconds else None)
            or realtime_signaling_guard.quota_cooldown_seconds
        )
        access_token = realtime_signaling_guard.mark_quota_exhausted(
            identity_key,
            attempt_id,
            cooldown_seconds,
        )
        if not access_token:
            return _error_response(
                status_code=404,
                code="realtime_attempt_not_found",
                message="Realtime attempt is unknown or expired",
                request_id=uuid.uuid4().hex,
                retryable=False,
            )
        account_service.mark_realtime_unavailable(
            access_token,
            status="limited",
            reason=report.reason if report else "quota_exhausted",
            cooldown_seconds=cooldown_seconds,
            restore_at=restore_at,
        )
        logger.info(
            f"[realtime] Voice quota isolation persisted: identity={identity.get('name')}, "
            f"cooldown_seconds={cooldown_seconds}"
        )
        return {
            "ok": True,
            "cooldown_seconds": cooldown_seconds,
            "restore_at": restore_at
            or (datetime.now(timezone.utc) + timedelta(seconds=cooldown_seconds)).isoformat(),
        }

    @router.post("/v1/realtime/sessions/{attempt_id}/text")
    async def send_realtime_text(
        attempt_id: str,
        body: RealtimeTextInput,
        authorization: str | None = Header(default=None),
    ):
        """向活跃的实时语音会话注入文字消息（通过 ChatGPT conversation API）。"""
        identity = require_identity(authorization)
        identity_key = str(identity.get("id") or identity.get("name") or "anonymous")
        request_id = uuid.uuid4().hex

        access_token = realtime_signaling_guard.get_attempt_token(identity_key, attempt_id)
        if not access_token:
            return _error_response(
                status_code=404,
                code="realtime_attempt_not_found",
                message="Realtime attempt is unknown or expired",
                request_id=request_id,
                retryable=False,
            )

        from services.openai_backend_api import OpenAIBackendAPI
        from utils.helper import new_uuid

        def _stream_text():
            api = OpenAIBackendAPI(access_token)
            try:
                from utils.helper import iter_sse_payloads
                for chunk in api.stream_conversation(
                    messages=[{"role": "user", "content": body.text}],
                    model="auto",
                ):
                    yield f"data: {chunk}\n\n"
            except Exception as exc:
                yield f"data: {json.dumps({'type': 'error', 'error': {'message': str(exc)[:200], 'code': 'internal_error'}})}\n\n"
            finally:
                api.close()

        import json
        return StreamingResponse(
            _stream_text(),
            media_type="text/event-stream",
            headers={"X-Request-ID": request_id, "Cache-Control": "no-cache"},
        )

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
            account_available_callback=account_service.mark_realtime_available,
            account_limited_callback=lambda token: account_service.mark_realtime_unavailable(
                token,
                status="limited",
                reason="quota_exhausted",
                cooldown_seconds=realtime_signaling_guard.quota_cooldown_seconds,
            ),
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
