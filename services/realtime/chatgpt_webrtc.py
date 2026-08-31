from __future__ import annotations

import json
import uuid
from typing import Any

from aiortc import RTCPeerConnection, RTCSessionDescription

from services.realtime.audio_track import BufferedAudioStreamTrack
from services.realtime.signaling import UpstreamSignalingError

REALTIME_DEFAULT_VOICE = "ember"
REALTIME_VOICES: tuple[dict[str, str], ...] = (
    {"id": "ember", "name": "Ember", "description": "自信乐观"},
    {"id": "glimmer", "name": "Sol", "description": "聪慧随性"},
    {"id": "breeze", "name": "Breeze", "description": "活泼认真"},
    {"id": "cove", "name": "Cove", "description": "沉稳直率"},
    {"id": "juniper", "name": "Juniper", "description": "开放豁达"},
    {"id": "maple", "name": "Maple", "description": "开朗直率"},
    {"id": "orbit", "name": "Spruce", "description": "冷静坚定"},
    {"id": "vale", "name": "Vale", "description": "聪颖好奇"},
    {"id": "fathom", "name": "Arbor", "description": "随和多才"},
)
REALTIME_VOICE_IDS = frozenset(voice["id"] for voice in REALTIME_VOICES)

# OpenAI 官方 Realtime API 声音名 → ChatGPT Web Voice 声音 id。客户端可以
# 直接使用官方声音名（例如 "marin"），未来切换到 api.openai.com 时无需改动
# 传参；映射按音色气质就近选择，允许多个官方名落在同一个上游声音上。
OFFICIAL_VOICE_ALIASES: dict[str, str] = {
    "marin": "ember",
    "cedar": "fathom",
    "alloy": "breeze",
    "ash": "cove",
    "ballad": "maple",
    "coral": "juniper",
    "echo": "orbit",
    "sage": "vale",
    "shimmer": "glimmer",
    "verse": "ember",
}


def resolve_voice(value: str | None) -> str | None:
    """把官方声音名或原生声音 id 解析为上游可用的声音 id；未知返回 None。"""
    normalized = (value or "").strip().lower()
    if not normalized:
        return REALTIME_DEFAULT_VOICE
    if normalized in REALTIME_VOICE_IDS:
        return normalized
    return OFFICIAL_VOICE_ALIASES.get(normalized)


def official_aliases_for(voice_id: str) -> list[str]:
    """列出映射到指定上游声音的官方声音名，用于 /voices 列表。"""
    return sorted(
        alias for alias, target in OFFICIAL_VOICE_ALIASES.items() if target == voice_id
    )


def build_session_config(
    voice: str = REALTIME_DEFAULT_VOICE,
    language: str = "auto",
    conversation_id: str | None = None,
    parent_message_id: str | None = None,
) -> dict[str, Any]:
    """Build the Web Voice session payload.

    Conversation continuity is intentionally represented at the top level,
    matching the web client's realtime contract.  Empty values are omitted so
    a new voice session keeps the upstream default conversation bootstrap.
    """
    config: dict[str, Any] = {
        "backend_reasoning_effort": "instant",
        "language_code": language,
        "requested_default_model": "",
        "voice": voice,
        "voice_session_id": str(uuid.uuid4()).upper(),
        "voice_status_request_id": str(uuid.uuid4()).upper(),
        "timezone_offset_min": -480,
        "timezone": "Asia/Shanghai",
        "voice_mode": "wingman",
        "model_slug": "",
        "model_slug_advanced": "",
        "client_tools": [],
        "history_and_training_disabled": False,
        "conversation_mode": {"kind": "primary_assistant"},
        "chat_mode": "chat",
        "enable_message_streaming": True,
    }
    if conversation_id:
        config["conversation_id"] = conversation_id
    if parent_message_id:
        config["parent_message_id"] = parent_message_id
    return config


def build_realtime_headers(access_token: str) -> dict[str, str]:
    return {
        "authorization": f"Bearer {access_token}",
        "oai-language": "zh-CN",
        "oai-device-id": str(uuid.uuid4()),
        "oai-session-id": str(uuid.uuid4()),
        "oai-client-build-number": "9006650",
        "oai-client-version": "prod-4750ed10e7af5b0895a0bd846a509225631c24f2",
        "x-openai-target-path": "/realtime/wm",
        "x-openai-target-route": "/realtime/wm",
        "user-agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
        ),
        "referer": "https://chatgpt.com/",
        "origin": "https://chatgpt.com",
    }


async def exchange_realtime_sdp(
    access_token: str,
    offer_sdp: str,
    voice: str = "ember",
    language: str = "auto",
    conversation_id: str | None = None,
    parent_message_id: str | None = None,
) -> tuple[str, str]:
    """将浏览器或 aiortc 的 SDP offer 代理给 ChatGPT，返回 answer 和位置。"""
    import asyncio
    from curl_cffi import CurlMime
    from curl_cffi import requests as cffi_requests

    session_cfg = build_session_config(
        voice=voice,
        language=language,
        conversation_id=conversation_id,
        parent_message_id=parent_message_id,
    )
    mime = CurlMime()
    mime.addpart(name="sdp", data=offer_sdp.encode())
    mime.addpart(
        name="session",
        data=json.dumps(session_cfg).encode(),
        content_type="application/json",
    )

    try:
        response = await asyncio.to_thread(
            cffi_requests.post,
            "https://chatgpt.com/realtime/wm",
            params={"dcid": "0"},
            multipart=mime,
            headers=build_realtime_headers(access_token),
            impersonate="chrome",
            timeout=30,
        )
    except Exception as exc:
        raise UpstreamSignalingError(0, str(exc)[:500]) from exc
    if response.status_code != 201:
        retry_after = response.headers.get("retry-after")
        try:
            retry_after_seconds = max(1, int(float(retry_after))) if retry_after else None
        except (TypeError, ValueError):
            retry_after_seconds = None
        raise UpstreamSignalingError(response.status_code, response.text[:500], retry_after_seconds)
    return response.text.strip(), response.headers.get("location", "")


async def create_peer_connection(
    access_token: str,
    voice: str = "ember",
    language: str = "auto",
) -> tuple[RTCPeerConnection, BufferedAudioStreamTrack, object, str]:
    """创建 WebRTC PeerConnection 并完成与 ChatGPT 的信令交换。

    Returns:
        (pc, input_audio_track, data_channel, session_location)
    """
    pc = RTCPeerConnection()
    input_track = BufferedAudioStreamTrack()
    pc.addTrack(input_track)
    pc.addTransceiver("video", direction="sendonly")
    dc = pc.createDataChannel("", negotiated=True, id=0)

    # 预注册 track 事件 — 必须在 setRemoteDescription 之前
    remote_audio_track_holder = []

    @pc.on("track")
    def _on_track(track):
        if track.kind == "audio":
            remote_audio_track_holder.append(track)

    offer = await pc.createOffer()
    await pc.setLocalDescription(offer)

    try:
        answer_sdp, location = await exchange_realtime_sdp(
            access_token=access_token,
            offer_sdp=pc.localDescription.sdp,
            voice=voice,
            language=language,
        )
    except Exception:
        await pc.close()
        raise

    await pc.setRemoteDescription(RTCSessionDescription(sdp=answer_sdp, type="answer"))

    remote_audio = remote_audio_track_holder[0] if remote_audio_track_holder else None
    return pc, input_track, dc, remote_audio, location
