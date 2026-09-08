from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import (
    APIRouter,
    Header,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field, field_validator

from api.support import extract_bearer_token, require_identity
from services.account_service import account_service
from services.realtime.session import CHATGPT_WEB_REALTIME_MODEL, RealtimeSession
from services.realtime.signaling import (
    SignalingBusyError,
    UpstreamSignalingError,
    realtime_signaling_guard,
)
from services.realtime.chatgpt_webrtc import (
    REALTIME_DEFAULT_VOICE,
    REALTIME_VOICE_IDS,  # noqa: F401  (re-export，测试与外部代码依赖)
    REALTIME_VOICES,
    exchange_realtime_sdp,
    official_aliases_for,
    resolve_voice,
)
from utils.log import logger

_LANGUAGE_PATTERN = re.compile(r"^(auto|[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8}){0,2})$")
_ATTEMPT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_SDP_MIN_LENGTH = 100
_SDP_MAX_LENGTH = 100_000


class RealtimeOffer(BaseModel):
    sdp: str = Field(min_length=_SDP_MIN_LENGTH, max_length=_SDP_MAX_LENGTH)
    voice: str = Field(default=REALTIME_DEFAULT_VOICE, min_length=1, max_length=32)
    language: str = Field(default="auto", pattern=r"^(auto|[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8}){0,2})$")
    attempt_id: str | None = Field(default=None, min_length=16, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    conversation_id: str | None = Field(default=None, max_length=100)
    parent_message_id: str | None = Field(default=None, max_length=100)
    # ``resume_handle`` is the public name used by the browser.  Keep
    # ``session_handle`` as an input alias for clients that use the response
    # field verbatim; both values are opaque and identity-bound server-side.
    resume_handle: str | None = Field(default=None, max_length=256)
    session_handle: str | None = Field(default=None, max_length=256)

    @field_validator("voice")
    @classmethod
    def validate_voice(cls, value: str) -> str:
        resolved = resolve_voice(value)
        if resolved is None:
            raise ValueError(f"unsupported voice: {value}")
        return resolved


class RealtimeTextInput(BaseModel):
    text: str = Field(min_length=1, max_length=10_000)
    conversation_id: str = Field(min_length=10, max_length=100)
    parent_message_id: str | None = Field(default=None, max_length=100)
    session_handle: str | None = Field(default=None, max_length=256)
    resume_handle: str | None = Field(default=None, max_length=256)


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


class RealtimeClientSecretRequest(BaseModel):
    """OpenAI GA 形状的 ``POST /v1/realtime/client_secrets`` 请求体。

    ``session`` 与官方保持同构（宽松解析），本项目专有的续接字段放在
    ``session.chatgpt2api`` 命名空间内，切换官方 API 时删除该字段即可。
    """

    expires_after: dict | None = None
    session: dict | None = None


def _error_type_for_status(status_code: int) -> str:
    if status_code == 429:
        return "rate_limit_error"
    if 400 <= status_code < 500:
        return "invalid_request_error"
    return "server_error"


def _error_response(
    *,
    status_code: int,
    code: str,
    message: str,
    request_id: str,
    retryable: bool,
    retry_after: int | None = None,
    attempt_id: str | None = None,
    param: str | None = None,
) -> JSONResponse:
    error: dict[str, object] = {
        "message": message,
        "type": _error_type_for_status(status_code),
        "code": code,
        "retryable": retryable,
        "request_id": request_id,
    }
    if param:
        error["param"] = param
    headers = {"X-Request-ID": request_id}
    if retry_after is not None:
        error["retry_after_ms"] = retry_after * 1000
        headers["Retry-After"] = str(retry_after)
    if attempt_id:
        headers["X-Attempt-Id"] = attempt_id
    payload: dict[str, object] = {"error": error}
    if attempt_id:
        payload["attempt_id"] = attempt_id
    return JSONResponse(payload, status_code=status_code, headers=headers)


class SessionConfigError(ValueError):
    """OpenAI 形状 session 配置中的字段无法接受。"""

    def __init__(self, message: str, param: str):
        super().__init__(message)
        self.param = param


def parse_session_options(session: object) -> dict[str, str]:
    """把 OpenAI GA 形状的 session 配置解析为内部信令选项。

    支持的字段：
    - ``audio.output.voice``（或兼容 beta 的顶层 ``voice``）——官方声音名
      与原生声音 id 均可；
    - ``audio.input.transcription.language``（或扩展里的 ``language``）；
    - ``model``——仅回显，上游模型不可配置；
    - ``chatgpt2api`` 扩展命名空间——``attempt_id`` /
      ``conversation_id`` / ``parent_message_id`` / ``resume_handle``。
    未知字段一律忽略，保证官方 SDK 生成的 payload 不会被拒绝。
    """
    if not isinstance(session, dict):
        return {}
    options: dict[str, str] = {}

    voice_requested = session.get("voice")
    audio = session.get("audio")
    if isinstance(audio, dict):
        output = audio.get("output")
        if isinstance(output, dict) and output.get("voice"):
            voice_requested = output.get("voice")
        audio_input = audio.get("input")
        if isinstance(audio_input, dict):
            transcription = audio_input.get("transcription")
            if isinstance(transcription, dict) and transcription.get("language"):
                options["language"] = str(transcription["language"]).strip()

    if voice_requested is not None:
        requested = str(voice_requested).strip()
        resolved = resolve_voice(requested)
        if resolved is None:
            raise SessionConfigError(
                f"unsupported voice: {requested}", param="session.audio.output.voice"
            )
        options["voice"] = resolved
        options["voice_requested"] = requested

    if session.get("model"):
        options["model_requested"] = str(session["model"]).strip()

    extension = session.get("chatgpt2api")
    if isinstance(extension, dict):
        if not options.get("language") and extension.get("language"):
            options["language"] = str(extension["language"]).strip()
        attempt_id = str(extension.get("attempt_id") or "").strip()
        if attempt_id:
            if not _ATTEMPT_ID_PATTERN.fullmatch(attempt_id):
                raise SessionConfigError(
                    "attempt_id must match ^[A-Za-z0-9_-]{16,64}$",
                    param="session.chatgpt2api.attempt_id",
                )
            options["attempt_id"] = attempt_id
        conversation_id = str(extension.get("conversation_id") or "").strip()
        if conversation_id:
            if len(conversation_id) > 100:
                raise SessionConfigError(
                    "conversation_id is too long", param="session.chatgpt2api.conversation_id"
                )
            options["conversation_id"] = conversation_id
        parent_message_id = str(extension.get("parent_message_id") or "").strip()
        if parent_message_id:
            if len(parent_message_id) > 100:
                raise SessionConfigError(
                    "parent_message_id is too long",
                    param="session.chatgpt2api.parent_message_id",
                )
            options["parent_message_id"] = parent_message_id
        resume_handle = str(
            extension.get("resume_handle") or extension.get("session_handle") or ""
        ).strip()
        if resume_handle:
            if len(resume_handle) > 256:
                raise SessionConfigError(
                    "resume_handle is too long", param="session.chatgpt2api.resume_handle"
                )
            options["resume_handle"] = resume_handle

    language = options.get("language", "")
    if language and not _LANGUAGE_PATTERN.fullmatch(language):
        raise SessionConfigError(
            f"unsupported language: {language}",
            param="session.audio.input.transcription.language",
        )
    return options


def _conversation_current_node(value: object) -> str:
    """Extract a conversation's current node without trusting arbitrary ids."""
    if not isinstance(value, dict):
        return ""
    for key in ("current_node", "currentNode"):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    # A few backend deployments wrap the conversation document one level deep.
    for key in ("conversation", "data"):
        nested = value.get(key)
        current = _conversation_current_node(nested)
        if current:
            return current
    return ""


def _latest_parent_message_id(value: object) -> str:
    """Read the assistant message cursor emitted by a conversation SSE event."""
    if not isinstance(value, dict):
        return ""
    for key in ("message", "item"):
        message = value.get(key)
        if isinstance(message, dict):
            author = message.get("author")
            role = author.get("role") if isinstance(author, dict) else message.get("role")
            # Avoid moving the cursor to an input/tool item.  Some upstream
            # events omit role, in which case the message id is still useful.
            if role is None or str(role).strip().lower() == "assistant":
                candidate = message.get("id") or message.get("message_id")
                if isinstance(candidate, str) and candidate.strip():
                    return candidate.strip()

    for key in ("parent_message_id", "parentMessageId"):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()

    nested = value.get("v")
    if isinstance(nested, dict):
        return _latest_parent_message_id(nested)
    return ""


@dataclass
class _CallSuccess:
    answer_sdp: str
    upstream_location: str
    attempt_id: str
    request_id: str
    session_handle: str


async def _negotiate_call(
    identity: dict,
    *,
    sdp: str,
    voice: str,
    language: str,
    attempt_id: str | None,
    conversation_id: str,
    parent_message_id: str,
    resume_handle: str,
) -> _CallSuccess | JSONResponse:
    """执行一次 SDP 信令交换；媒体随后在客户端与 ChatGPT 之间直连。

    新旧两代 HTTP 端点共享这段核心逻辑，只是响应封装不同。
    """
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
    attempt_id, excluded = realtime_signaling_guard.open_attempt(identity_key, attempt_id)
    access_token = ""
    try:
        if resume_handle:
            # A resume is deliberately pinned to the original account so
            # a text-primed voice conversation remains on one upstream
            # account.  The opaque handle is checked against identity and
            # never gives the caller access to the token itself.
            access_token = realtime_signaling_guard.resume_session(
                identity_key,
                resume_handle,
                conversation_id=conversation_id,
            ) or ""
            if not access_token:
                return _error_response(
                    status_code=404,
                    code="realtime_session_handle_not_found",
                    message="Realtime session handle is unknown or expired",
                    request_id=request_id,
                    retryable=False,
                    attempt_id=attempt_id,
                )
        else:
            access_token = account_service.get_realtime_access_token(excluded)
        async with realtime_signaling_guard.signaling_slot():
            realtime_signaling_guard.record_account(attempt_id, access_token)
            exchange_kwargs = {
                "access_token": access_token,
                "offer_sdp": sdp,
                "voice": voice,
                "language": language,
            }
            if conversation_id:
                exchange_kwargs["conversation_id"] = conversation_id
            if parent_message_id:
                exchange_kwargs["parent_message_id"] = parent_message_id
            answer_sdp, location = await exchange_realtime_sdp(
                **exchange_kwargs,
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
    session_handle = realtime_signaling_guard.pin_session(
        identity_key,
        access_token,
        conversation_id=conversation_id,
        session_id=resume_handle or None,
    )
    logger.info(
        f"[realtime] Direct WebRTC session: request_id={request_id}, voice={voice}, "
        f"identity={identity.get('name')}"
    )
    return _CallSuccess(
        answer_sdp=answer_sdp,
        upstream_location=location,
        attempt_id=attempt_id,
        request_id=request_id,
        session_handle=session_handle,
    )


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
            "protocol_version": 2,
            "transports": {
                "webrtc": {
                    "client_secrets_url": "/v1/realtime/client_secrets",
                    "calls_url": "/v1/realtime/calls",
                    "legacy_signaling_url": "/v1/realtime/sessions",
                    "recommended": True,
                },
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
            "text_input_url": "/v1/realtime/calls/{call_id}/text",
            "quota_report_url": "/v1/realtime/calls/{call_id}/quota-exhausted",
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
                {
                    **voice,
                    "object": "realtime.voice",
                    "aliases": official_aliases_for(voice["id"]),
                    "preview_url": f"/audio/voice-previews/{voice['id']}.m4a",
                }
                for voice in REALTIME_VOICES
            ],
        }

    @router.post("/v1/realtime/client_secrets")
    async def create_realtime_client_secret(
        body: RealtimeClientSecretRequest | None = None,
        authorization: str | None = Header(default=None),
    ):
        """签发短时效 ephemeral key（``ek_...``），供 ``/v1/realtime/calls`` 使用。

        请求/响应形状对齐 OpenAI GA 的 ``POST /v1/realtime/client_secrets``。
        """
        identity = require_identity(authorization)
        identity_key = str(identity.get("id") or identity.get("name") or "anonymous")
        request_id = uuid.uuid4().hex
        payload = body or RealtimeClientSecretRequest()
        try:
            options = parse_session_options(payload.session)
        except SessionConfigError as exc:
            return _error_response(
                status_code=400,
                code="invalid_value",
                message=str(exc),
                request_id=request_id,
                retryable=False,
                param=exc.param,
            )
        ttl_seconds: int | None = None
        if isinstance(payload.expires_after, dict):
            seconds = payload.expires_after.get("seconds")
            if isinstance(seconds, (int, float)) and seconds > 0:
                ttl_seconds = int(seconds)
        secret, ttl = realtime_signaling_guard.mint_client_secret(
            identity_key,
            identity,
            session_options=options,
            ttl_seconds=ttl_seconds,
        )
        voice = options.get("voice", REALTIME_DEFAULT_VOICE)
        return JSONResponse(
            {
                "value": secret,
                "expires_at": int(time.time()) + ttl,
                "session": {
                    "type": "realtime",
                    "object": "realtime.session",
                    "model": CHATGPT_WEB_REALTIME_MODEL,
                    "audio": {"output": {"voice": options.get("voice_requested") or voice}},
                },
            },
            headers={"X-Request-ID": request_id},
        )

    @router.post("/v1/realtime/calls")
    async def create_realtime_call(
        request: Request,
        authorization: str | None = Header(default=None),
    ):
        """交换 WebRTC SDP，形状对齐 OpenAI GA 的 ``POST /v1/realtime/calls``。

        请求体支持 ``application/sdp``（裸 SDP）和 ``multipart/form-data``
        （``sdp`` + 可选 ``session`` JSON，即官方 unified interface 形状）。
        鉴权支持 ephemeral key（``ek_...``）和普通 API Key。成功响应为
        ``application/sdp``，call id 在 ``Location`` 响应头返回。
        """
        request_id = uuid.uuid4().hex
        bearer = extract_bearer_token(authorization)
        secret_record = realtime_signaling_guard.redeem_client_secret(bearer)
        if secret_record is not None:
            identity = secret_record.identity
            options = dict(secret_record.session_options)
        else:
            identity = require_identity(authorization)
            options = {}

        content_type = (request.headers.get("content-type") or "").lower()
        sdp = ""
        if "multipart/form-data" in content_type or "application/x-www-form-urlencoded" in content_type:
            form = await request.form()
            sdp = str(form.get("sdp") or "")
            session_raw = form.get("session")
            if session_raw:
                try:
                    session_config = json.loads(str(session_raw))
                except (TypeError, json.JSONDecodeError):
                    return _error_response(
                        status_code=400,
                        code="invalid_value",
                        message="session form field must be valid JSON",
                        request_id=request_id,
                        retryable=False,
                        param="session",
                    )
                try:
                    # multipart 里显式提供的字段优先于 ephemeral key 预存配置。
                    options.update(parse_session_options(session_config))
                except SessionConfigError as exc:
                    return _error_response(
                        status_code=400,
                        code="invalid_value",
                        message=str(exc),
                        request_id=request_id,
                        retryable=False,
                        param=exc.param,
                    )
        else:
            # 官方浏览器路径：裸 SDP。宽松接受缺失/文本类 content-type。
            sdp = (await request.body()).decode("utf-8", errors="replace")

        if not (_SDP_MIN_LENGTH <= len(sdp) <= _SDP_MAX_LENGTH):
            return _error_response(
                status_code=400,
                code="invalid_value",
                message="request body must contain a WebRTC SDP offer",
                request_id=request_id,
                retryable=False,
                param="sdp",
            )

        outcome = await _negotiate_call(
            identity,
            sdp=sdp,
            voice=options.get("voice", REALTIME_DEFAULT_VOICE),
            language=options.get("language", "auto"),
            attempt_id=options.get("attempt_id"),
            conversation_id=options.get("conversation_id", ""),
            parent_message_id=options.get("parent_message_id", ""),
            resume_handle=options.get("resume_handle", ""),
        )
        if isinstance(outcome, JSONResponse):
            return outcome
        return Response(
            content=outcome.answer_sdp,
            status_code=201,
            media_type="application/sdp",
            headers={
                "Location": f"/v1/realtime/calls/{outcome.attempt_id}",
                "X-Request-ID": outcome.request_id,
                "X-Attempt-Id": outcome.attempt_id,
                "X-Session-Handle": outcome.session_handle,
                "X-Resume-Handle": outcome.session_handle,
            },
        )

    @router.post("/v1/realtime/sessions")
    async def create_realtime_session(
        offer: RealtimeOffer,
        authorization: str | None = Header(default=None),
    ):
        """旧版 JSON 信令端点（deprecated）；新集成请使用 /v1/realtime/calls。"""
        identity = require_identity(authorization)
        outcome = await _negotiate_call(
            identity,
            sdp=offer.sdp,
            voice=offer.voice,
            language=offer.language,
            attempt_id=offer.attempt_id,
            conversation_id=(offer.conversation_id or "").strip(),
            parent_message_id=(offer.parent_message_id or "").strip(),
            resume_handle=(offer.resume_handle or offer.session_handle or "").strip(),
        )
        if isinstance(outcome, JSONResponse):
            return outcome
        return JSONResponse(
            {
                "sdp": outcome.answer_sdp,
                "location": outcome.upstream_location,
                "attempt_id": outcome.attempt_id,
                "request_id": outcome.request_id,
                "session_handle": outcome.session_handle,
                "resume_handle": outcome.session_handle,
            },
            headers={"X-Request-ID": outcome.request_id},
        )

    async def _report_quota_exhausted(
        call_id: str,
        report: RealtimeQuotaReport | None,
        authorization: str | None,
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
            call_id,
            cooldown_seconds,
        )
        if not access_token:
            return _error_response(
                status_code=404,
                code="realtime_attempt_not_found",
                message="Realtime call is unknown or expired",
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

    @router.post("/v1/realtime/calls/{call_id}/quota-exhausted")
    async def report_realtime_call_quota_exhausted(
        call_id: str,
        report: RealtimeQuotaReport | None = None,
        authorization: str | None = Header(default=None),
    ):
        return await _report_quota_exhausted(call_id, report, authorization)

    @router.post("/v1/realtime/sessions/{attempt_id}/quota-exhausted")
    async def report_realtime_quota_exhausted(
        attempt_id: str,
        report: RealtimeQuotaReport | None = None,
        authorization: str | None = Header(default=None),
    ):
        """旧路径（deprecated）；与 /v1/realtime/calls/{call_id}/quota-exhausted 等价。"""
        return await _report_quota_exhausted(attempt_id, report, authorization)

    async def _send_realtime_text(
        call_id: str,
        body: RealtimeTextInput,
        authorization: str | None,
    ):
        """向活跃的实时语音会话注入文字消息（通过 ChatGPT conversation API）。"""
        identity = require_identity(authorization)
        identity_key = str(identity.get("id") or identity.get("name") or "anonymous")
        request_id = uuid.uuid4().hex

        requested_handle = (body.session_handle or body.resume_handle or "").strip()
        if requested_handle:
            access_token = realtime_signaling_guard.get_pinned_token(
                identity_key,
                requested_handle,
                conversation_id=body.conversation_id,
                refresh=True,
            )
        else:
            access_token = realtime_signaling_guard.get_attempt_token(identity_key, call_id)
        if not access_token:
            return _error_response(
                status_code=404,
                code=("realtime_session_handle_not_found" if requested_handle else "realtime_attempt_not_found"),
                message=(
                    "Realtime session handle is unknown or expired"
                    if requested_handle else "Realtime call is unknown or expired"
                ),
                request_id=request_id,
                retryable=False,
            )

        from services.openai_backend_api import OpenAIBackendAPI

        def _stream_text():
            api = OpenAIBackendAPI(access_token)
            latest_parent_message_id = (body.parent_message_id or "").strip()
            saw_done = False
            try:
                if not latest_parent_message_id:
                    try:
                        latest_parent_message_id = _conversation_current_node(
                            api._get_conversation(body.conversation_id)
                        )
                    except Exception as exc:
                        # A stale/missing conversation document should not
                        # prevent the normal backend stream from attempting a
                        # root parent.  Keep the failure out of the client
                        # payload because it can contain upstream details.
                        logger.debug(
                            "[realtime] Could not resolve conversation current_node: %s",
                            exc.__class__.__name__,
                        )
                for chunk in api.stream_conversation(
                    messages=[{"role": "user", "content": body.text}],
                    model="auto",
                    conversation_id=body.conversation_id,
                    parent_message_id=latest_parent_message_id or None,
                ):
                    if chunk == "[DONE]":
                        saw_done = True
                        continue
                    serialized = chunk
                    try:
                        decoded = json.loads(chunk)
                    except (TypeError, json.JSONDecodeError):
                        decoded = None
                    if isinstance(decoded, dict):
                        candidate = _latest_parent_message_id(decoded)
                        if candidate:
                            latest_parent_message_id = candidate
                        # Make the cursor available on the same SSE event so
                        # clients need not reverse-engineer each upstream shape.
                        if latest_parent_message_id:
                            decoded["parent_message_id"] = latest_parent_message_id
                            serialized = json.dumps(decoded, ensure_ascii=False)
                    yield f"data: {serialized}\n\n"

                # Emit a stable terminal cursor event.  This is intentionally
                # metadata-only: no upstream token or account detail crosses
                # the API boundary.
                if latest_parent_message_id:
                    yield (
                        "data: "
                        + json.dumps(
                            {
                                "type": "realtime.text.completed",
                                "conversation_id": body.conversation_id,
                                "parent_message_id": latest_parent_message_id,
                            },
                            ensure_ascii=False,
                        )
                        + "\n\n"
                    )
                if saw_done:
                    yield "data: [DONE]\n\n"
            except Exception as exc:
                yield f"data: {json.dumps({'type': 'error', 'error': {'message': str(exc)[:200], 'code': 'internal_error'}})}\n\n"
            finally:
                api.close()

        return StreamingResponse(
            _stream_text(),
            media_type="text/event-stream",
            headers={"X-Request-ID": request_id, "Cache-Control": "no-cache"},
        )

    @router.post("/v1/realtime/calls/{call_id}/text")
    async def send_realtime_call_text(
        call_id: str,
        body: RealtimeTextInput,
        authorization: str | None = Header(default=None),
    ):
        return await _send_realtime_text(call_id, body, authorization)

    @router.post("/v1/realtime/sessions/{attempt_id}/text")
    async def send_realtime_text(
        attempt_id: str,
        body: RealtimeTextInput,
        authorization: str | None = Header(default=None),
    ):
        """旧路径（deprecated）；与 /v1/realtime/calls/{call_id}/text 等价。"""
        return await _send_realtime_text(attempt_id, body, authorization)

    @router.websocket("/v1/realtime")
    async def realtime_endpoint(
        websocket: WebSocket,
        model: str = Query(default=CHATGPT_WEB_REALTIME_MODEL),
        voice: str = Query(default=REALTIME_DEFAULT_VOICE),
    ):
        accepted_subprotocol: str | None = None
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
                    # Browsers abort the handshake when they offer a protocol and
                    # the server does not echo the selected value in the response.
                    accepted_subprotocol = proto
                    break

        try:
            identity = require_identity(auth)
        except HTTPException:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return

        resolved_voice = resolve_voice(voice)
        if resolved_voice is None:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="unsupported voice")
            return
        voice = resolved_voice

        # 获取 ChatGPT access_token
        try:
            access_token = account_service.get_realtime_access_token()
        except RuntimeError as e:
            await websocket.accept(subprotocol=accepted_subprotocol)
            import json
            await websocket.send_text(json.dumps({
                "type": "error",
                "error": {"message": str(e), "code": "no_account"}
            }))
            await websocket.close(code=1011)
            return

        await websocket.accept(subprotocol=accepted_subprotocol)
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
