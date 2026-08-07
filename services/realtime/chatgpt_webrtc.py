from __future__ import annotations

import json
import uuid
from typing import Any

from aiortc import RTCPeerConnection, RTCSessionDescription

from services.realtime.audio_track import BufferedAudioStreamTrack


def build_session_config(voice: str = "ember", language: str = "auto") -> dict[str, Any]:
    return {
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


async def create_peer_connection(
    access_token: str,
    voice: str = "ember",
    language: str = "auto",
) -> tuple[RTCPeerConnection, BufferedAudioStreamTrack, object, str]:
    """创建 WebRTC PeerConnection 并完成与 ChatGPT 的信令交换。

    Returns:
        (pc, input_audio_track, data_channel, session_location)
    """
    import asyncio
    from curl_cffi import requests as cffi_requests
    from curl_cffi import CurlMime

    pc = RTCPeerConnection()
    input_track = BufferedAudioStreamTrack()
    pc.addTrack(input_track)
    pc.addTransceiver("video", direction="sendonly")
    dc = pc.createDataChannel("oai-events")

    # 预注册 track 事件 — 必须在 setRemoteDescription 之前
    remote_audio_track_holder = []

    @pc.on("track")
    def _on_track(track):
        if track.kind == "audio":
            remote_audio_track_holder.append(track)

    offer = await pc.createOffer()
    await pc.setLocalDescription(offer)

    session_cfg = build_session_config(voice=voice, language=language)
    headers = build_realtime_headers(access_token)

    mime = CurlMime()
    mime.addpart(name="sdp", data=pc.localDescription.sdp.encode())
    mime.addpart(name="session", data=json.dumps(session_cfg).encode(), content_type="application/json")

    resp = await asyncio.to_thread(
        cffi_requests.post,
        "https://chatgpt.com/realtime/wm",
        params={"dcid": "0"},
        multipart=mime,
        headers=headers,
        impersonate="chrome",
        timeout=30,
    )

    if resp.status_code != 201:
        await pc.close()
        raise RuntimeError(f"Realtime signaling failed: HTTP {resp.status_code} - {resp.text[:200]}")

    answer_sdp = resp.text.strip()
    location = resp.headers.get("location", "")

    await pc.setRemoteDescription(RTCSessionDescription(sdp=answer_sdp, type="answer"))

    remote_audio = remote_audio_track_holder[0] if remote_audio_track_holder else None
    return pc, input_track, dc, remote_audio, location
