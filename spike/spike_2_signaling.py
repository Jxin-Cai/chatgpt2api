"""
Spike 2: 用真实 access_token 向 ChatGPT POST /realtime/wm 获取 SDP answer。
验证 curl_cffi multipart 信令是否可行。
"""

import asyncio
import json
import sys
import uuid
from fractions import Fraction

from aiortc import RTCPeerConnection, RTCSessionDescription, MediaStreamTrack
from av import AudioFrame

sys.path.insert(0, ".")
from services.account_service import account_service


SAMPLE_RATE = 48000
SAMPLES_PER_FRAME = 960


class SilentAudioTrack(MediaStreamTrack):
    """静音音频轨道 — 仅用于生成 SDP offer 中的 audio m-line"""
    kind = "audio"

    def __init__(self):
        super().__init__()
        self._ts = 0

    async def recv(self):
        frame = AudioFrame(format="s16", layout="mono", samples=SAMPLES_PER_FRAME)
        frame.sample_rate = SAMPLE_RATE
        frame.pts = self._ts
        frame.time_base = Fraction(1, SAMPLE_RATE)
        frame.planes[0].update(b"\x00" * SAMPLES_PER_FRAME * 2)
        self._ts += SAMPLES_PER_FRAME
        await asyncio.sleep(0.02)
        return frame


def get_access_token() -> str:
    """从环境变量或账号池获取 token"""
    import os
    token = os.environ.get("CHATGPT_ACCESS_TOKEN", "")
    if not token:
        token = account_service.get_text_access_token(model="auto")
    if not token:
        raise RuntimeError("No access token available. Set CHATGPT_ACCESS_TOKEN env var.")
    print(f"[spike2] Got access_token: ...{token[-20:]}")
    return token


def build_session_config(voice: str = "ember") -> dict:
    return {
        "backend_reasoning_effort": "instant",
        "language_code": "auto",
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


def build_headers(access_token: str) -> dict:
    device_id = str(uuid.uuid4())
    session_id = str(uuid.uuid4())
    return {
        "authorization": f"Bearer {access_token}",
        "oai-language": "zh-CN",
        "oai-device-id": device_id,
        "oai-session-id": session_id,
        "oai-client-build-number": "9006650",
        "oai-client-version": "prod-4750ed10e7af5b0895a0bd846a509225631c24f2",
        "x-openai-target-path": "/realtime/wm",
        "x-openai-target-route": "/realtime/wm",
        "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
        "referer": "https://chatgpt.com/",
        "origin": "https://chatgpt.com",
    }


async def run():
    # 1. 获取 access_token
    access_token = get_access_token()

    # 2. 创建 aiortc PeerConnection 并生成 SDP offer
    pc = RTCPeerConnection()
    pc.addTrack(SilentAudioTrack())
    pc.addTransceiver("video", direction="sendonly")
    dc = pc.createDataChannel("oai-events")

    offer = await pc.createOffer()
    await pc.setLocalDescription(offer)

    offer_sdp = pc.localDescription.sdp
    print(f"[spike2] Generated SDP offer ({len(offer_sdp)} bytes)")
    print(f"[spike2] SDP contains audio: {'m=audio' in offer_sdp}")
    print(f"[spike2] SDP contains video: {'m=video' in offer_sdp}")
    print(f"[spike2] SDP contains datachannel: {'webrtc-datachannel' in offer_sdp}")

    # 3. POST to /realtime/wm via curl_cffi
    from curl_cffi import requests as cffi_requests
    from curl_cffi import CurlMime

    session_cfg = build_session_config()
    headers = build_headers(access_token)

    print(f"\n[spike2] POSTing to https://chatgpt.com/realtime/wm?dcid=0 ...")

    mime = CurlMime()
    mime.addpart(name="sdp", data=offer_sdp.encode())
    mime.addpart(
        name="session",
        data=json.dumps(session_cfg).encode(),
        content_type="application/json",
    )

    try:
        resp = cffi_requests.post(
            "https://chatgpt.com/realtime/wm",
            params={"dcid": "0"},
            multipart=mime,
            headers=headers,
            impersonate="chrome",
            timeout=30,
        )
    except Exception as e:
        print(f"[spike2] ✗ Request failed: {e}")
        await pc.close()
        return False

    print(f"[spike2] Response status: {resp.status_code}")
    print(f"[spike2] Response headers:")
    for key in ["content-type", "location", "x-request-id"]:
        if key in resp.headers:
            print(f"  {key}: {resp.headers[key]}")

    if resp.status_code != 201:
        print(f"[spike2] ✗ Expected 201, got {resp.status_code}")
        print(f"[spike2] Response body: {resp.text[:500]}")
        await pc.close()
        return False

    # 4. 验证 SDP answer
    answer_sdp = resp.text.strip()
    print(f"\n[spike2] SDP Answer received ({len(answer_sdp)} bytes)")
    print(f"[spike2] Answer contains 'a=ice-lite': {'a=ice-lite' in answer_sdp}")
    print(f"[spike2] Answer contains opus: {'opus' in answer_sdp}")
    print(f"[spike2] Answer contains 'realtimeapi': {'realtimeapi' in answer_sdp}")

    # 提取 ICE candidates
    candidates = [line for line in answer_sdp.split("\n") if line.startswith("a=candidate:")]
    print(f"[spike2] ICE candidates in answer: {len(candidates)}")
    for c in candidates[:3]:
        print(f"  {c.strip()}")

    # 5. 尝试 setRemoteDescription
    try:
        await pc.setRemoteDescription(RTCSessionDescription(sdp=answer_sdp, type="answer"))
        print(f"\n[spike2] ✓ setRemoteDescription succeeded")
        print(f"[spike2] Signaling state: {pc.signalingState}")
    except Exception as e:
        print(f"\n[spike2] ✗ setRemoteDescription failed: {e}")
        await pc.close()
        return False

    # Location header (WebRTC session reference)
    location = resp.headers.get("location", "")
    print(f"[spike2] Location: {location}")

    print("\n=== Spike 2 Results ===")
    print("✓ curl_cffi multipart POST to /realtime/wm succeeded")
    print("✓ Got 201 with valid SDP answer")
    print("✓ setRemoteDescription accepted the answer")
    print("✓ Signaling is stable — ready for ICE/DTLS connection")

    await pc.close()
    return True


if __name__ == "__main__":
    success = asyncio.run(run())
    print(f"\n{'SUCCESS' if success else 'FAILED'}")
    exit(0 if success else 1)
