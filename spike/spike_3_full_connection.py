"""
Spike 3: 完整 WebRTC 连接 — 验证 ICE/DTLS/SCTP 建立，观察 DataChannel 和音频轨道。
这是最关键的 spike：证明 aiortc 可以与 ChatGPT 服务端建立 P2P 连接。
"""

import asyncio
import json
import os
import sys
import uuid
from fractions import Fraction

from aiortc import RTCPeerConnection, RTCSessionDescription, MediaStreamTrack
from av import AudioFrame

sys.path.insert(0, ".")

SAMPLE_RATE = 48000
SAMPLES_PER_FRAME = 960


class SilentAudioTrack(MediaStreamTrack):
    """静音音频轨道"""
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
    access_token = os.environ.get("CHATGPT_ACCESS_TOKEN", "")
    if not access_token:
        print("Set CHATGPT_ACCESS_TOKEN env var")
        return False

    print(f"[spike3] Token: ...{access_token[-20:]}")

    # 1. 创建 PeerConnection
    pc = RTCPeerConnection()

    # 状态追踪
    events_log = []
    remote_tracks = []
    dc_messages_received = []
    dc_remote_channels = []
    connection_established = asyncio.Event()
    audio_frame_received = asyncio.Event()

    @pc.on("connectionstatechange")
    async def on_conn():
        state = pc.connectionState
        events_log.append(f"connectionState: {state}")
        print(f"[spike3] connectionState -> {state}")
        if state == "connected":
            connection_established.set()
        elif state == "failed":
            print("[spike3] ✗ Connection FAILED")
            connection_established.set()

    @pc.on("iceconnectionstatechange")
    async def on_ice():
        events_log.append(f"iceConnectionState: {pc.iceConnectionState}")
        print(f"[spike3] iceConnectionState -> {pc.iceConnectionState}")

    @pc.on("track")
    def on_track(track):
        print(f"[spike3] ✓ Remote track received: kind={track.kind}")
        remote_tracks.append(track)
        if track.kind == "audio":
            async def recv_audio():
                count = 0
                while count < 50:
                    try:
                        frame = await asyncio.wait_for(track.recv(), timeout=5)
                        count += 1
                        if count == 1:
                            print(f"[spike3] ✓ First audio frame: format={frame.format.name}, "
                                  f"samples={frame.samples}, rate={frame.sample_rate}")
                            audio_frame_received.set()
                        if count % 10 == 0:
                            print(f"[spike3] Audio frames received: {count}")
                    except asyncio.TimeoutError:
                        print(f"[spike3] Audio recv timeout after {count} frames")
                        break
                    except Exception as e:
                        print(f"[spike3] Audio recv error: {e}")
                        break
            asyncio.ensure_future(recv_audio())

    @pc.on("datachannel")
    def on_datachannel(channel):
        print(f"[spike3] ✓ Remote DataChannel: label={channel.label!r}, id={channel.id}, "
              f"ordered={channel.ordered}, protocol={channel.protocol!r}")
        dc_remote_channels.append(channel)

        @channel.on("message")
        def on_msg(msg):
            if isinstance(msg, str):
                dc_messages_received.append(msg)
                # 只打印前 500 字符
                preview = msg[:500]
                print(f"[spike3] DC recv ({len(msg)} bytes): {preview}")
            else:
                dc_messages_received.append(f"[binary {len(msg)} bytes]")
                print(f"[spike3] DC recv binary: {len(msg)} bytes")

    # 2. 添加 track 和 DataChannel
    audio_track = SilentAudioTrack()
    pc.addTrack(audio_track)
    pc.addTransceiver("video", direction="sendonly")
    local_dc = pc.createDataChannel("oai-events")

    local_dc_opened = asyncio.Event()

    @local_dc.on("open")
    def on_local_dc_open():
        print(f"[spike3] ✓ Local DataChannel 'oai-events' opened")
        local_dc_opened.set()

    @local_dc.on("message")
    def on_local_dc_msg(msg):
        if isinstance(msg, str):
            dc_messages_received.append(msg)
            preview = msg[:500]
            print(f"[spike3] Local DC recv ({len(msg)} bytes): {preview}")
        else:
            dc_messages_received.append(f"[binary {len(msg)} bytes]")
            print(f"[spike3] Local DC recv binary: {len(msg)} bytes")

    # 3. Create offer
    offer = await pc.createOffer()
    await pc.setLocalDescription(offer)
    print(f"[spike3] SDP offer generated ({len(pc.localDescription.sdp)} bytes)")

    # 4. POST to /realtime/wm
    from curl_cffi import requests as cffi_requests
    from curl_cffi import CurlMime

    session_cfg = build_session_config()
    headers = build_headers(access_token)
    mime = CurlMime()
    mime.addpart(name="sdp", data=pc.localDescription.sdp.encode())
    mime.addpart(name="session", data=json.dumps(session_cfg).encode(), content_type="application/json")

    print("[spike3] POST /realtime/wm ...")
    resp = cffi_requests.post(
        "https://chatgpt.com/realtime/wm",
        params={"dcid": "0"},
        multipart=mime,
        headers=headers,
        impersonate="chrome",
        timeout=30,
    )

    if resp.status_code != 201:
        print(f"[spike3] ✗ Signaling failed: {resp.status_code}")
        print(f"  Body: {resp.text[:300]}")
        await pc.close()
        return False

    print(f"[spike3] ✓ 201 OK, Location: {resp.headers.get('location', 'N/A')}")

    # 5. Set remote description
    answer_sdp = resp.text.strip()
    await pc.setRemoteDescription(RTCSessionDescription(sdp=answer_sdp, type="answer"))
    print(f"[spike3] ✓ Remote SDP set, signalingState={pc.signalingState}")

    # 6. 等待连接建立
    print("[spike3] Waiting for ICE/DTLS connection (max 15s)...")
    try:
        await asyncio.wait_for(connection_established.wait(), timeout=15)
    except asyncio.TimeoutError:
        print(f"[spike3] ✗ Connection timeout. State: {pc.connectionState}, ICE: {pc.iceConnectionState}")

    if pc.connectionState != "connected":
        print(f"[spike3] ✗ Not connected. Final state: {pc.connectionState}")
        await pc.close()
        return False

    print(f"\n[spike3] ✓✓✓ CONNECTION ESTABLISHED ✓✓✓")

    # 7. 等待 DataChannel 和音频
    print("[spike3] Waiting for DataChannel open (5s)...")
    try:
        await asyncio.wait_for(local_dc_opened.wait(), timeout=5)
    except asyncio.TimeoutError:
        print("[spike3] Local DC did not open in time")

    print("[spike3] Waiting for audio frames (10s)...")
    try:
        await asyncio.wait_for(audio_frame_received.wait(), timeout=10)
    except asyncio.TimeoutError:
        print("[spike3] No audio frames received (expected — ChatGPT waits for user speech)")

    # 等更多 DataChannel 消息
    await asyncio.sleep(3)

    # 8. 输出结果
    print("\n=== Spike 3 Results ===")
    print(f"Connection: {pc.connectionState}")
    print(f"ICE: {pc.iceConnectionState}")
    print(f"Remote tracks: {[t.kind for t in remote_tracks]}")
    print(f"Local DC opened: {local_dc_opened.is_set()}")
    print(f"Remote DCs received: {[(c.label, c.id) for c in dc_remote_channels]}")
    print(f"DC messages total: {len(dc_messages_received)}")
    for i, msg in enumerate(dc_messages_received[:10]):
        print(f"  [{i}] {msg[:200]}")
    print(f"Audio frames received: {audio_frame_received.is_set()}")
    print(f"Events: {events_log}")

    success = pc.connectionState == "connected"
    print(f"\n{'✓ SPIKE 3 SUCCESS' if success else '✗ SPIKE 3 FAILED'}")

    await pc.close()
    return success


if __name__ == "__main__":
    success = asyncio.run(run())
    exit(0 if success else 1)
