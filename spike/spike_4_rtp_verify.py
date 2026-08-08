"""
Spike 4: 验证 aiortc 是否真的在发送 RTP 包到 ChatGPT。
通过 getStats() 检查 outbound-rtp 的 packetsSent 和 bytesSent。
"""
import asyncio
import json
import os
import sys
import uuid
from fractions import Fraction

from aiortc import RTCPeerConnection, RTCSessionDescription, MediaStreamTrack
from av import AudioFrame
import numpy as np

sys.path.insert(0, ".")

SAMPLE_RATE = 48000
SAMPLES_PER_FRAME = 960


class ToneAudioTrack(MediaStreamTrack):
    """生成 440Hz 正弦波 — 保证是非静音有效音频"""
    kind = "audio"

    def __init__(self):
        super().__init__()
        self._pts = 0

    async def recv(self):
        t = np.linspace(self._pts / SAMPLE_RATE, (self._pts + SAMPLES_PER_FRAME) / SAMPLE_RATE, SAMPLES_PER_FRAME, endpoint=False)
        samples = (np.sin(2 * np.pi * 440 * t) * 16000).astype(np.int16)

        frame = AudioFrame(format="s16", layout="mono", samples=SAMPLES_PER_FRAME)
        frame.sample_rate = SAMPLE_RATE
        frame.pts = self._pts
        frame.time_base = Fraction(1, SAMPLE_RATE)
        frame.planes[0].update(samples.tobytes())
        self._pts += SAMPLES_PER_FRAME
        await asyncio.sleep(0.02)  # 20ms pacing
        return frame


async def run():
    access_token = os.environ.get("CHATGPT_ACCESS_TOKEN", "")
    if not access_token:
        print("Set CHATGPT_ACCESS_TOKEN")
        return

    pc = RTCPeerConnection()
    tone_track = ToneAudioTrack()
    pc.addTrack(tone_track)
    pc.addTransceiver("video", direction="sendonly")
    dc = pc.createDataChannel("oai-events")

    connection_ready = asyncio.Event()

    @pc.on("connectionstatechange")
    async def on_state():
        print(f"[state] {pc.connectionState}")
        if pc.connectionState == "connected":
            connection_ready.set()

    remote_non_silence = 0

    @pc.on("track")
    def on_track(track):
        if track.kind == "audio":
            async def recv_audio():
                nonlocal remote_non_silence
                count = 0
                while count < 500:
                    try:
                        frame = await asyncio.wait_for(track.recv(), timeout=2)
                        pcm = bytes(frame.planes[0])
                        if not all(b == 0 for b in pcm[:20]):
                            remote_non_silence += 1
                        count += 1
                    except:
                        break
            asyncio.ensure_future(recv_audio())

    offer = await pc.createOffer()
    await pc.setLocalDescription(offer)

    from curl_cffi import requests as cffi_requests
    from curl_cffi import CurlMime

    session_cfg = {
        "backend_reasoning_effort": "instant",
        "language_code": "auto",
        "requested_default_model": "",
        "voice": "ember",
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
    headers = {
        "authorization": f"Bearer {access_token}",
        "oai-language": "zh-CN",
        "oai-device-id": str(uuid.uuid4()),
        "oai-session-id": str(uuid.uuid4()),
        "oai-client-build-number": "9006650",
        "oai-client-version": "prod-4750ed10e7af5b0895a0bd846a509225631c24f2",
        "x-openai-target-path": "/realtime/wm",
        "x-openai-target-route": "/realtime/wm",
        "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
        "referer": "https://chatgpt.com/",
        "origin": "https://chatgpt.com",
    }

    mime = CurlMime()
    mime.addpart(name="sdp", data=pc.localDescription.sdp.encode())
    mime.addpart(name="session", data=json.dumps(session_cfg).encode(), content_type="application/json")

    resp = await asyncio.to_thread(cffi_requests.post, "https://chatgpt.com/realtime/wm",
        params={"dcid": "0"}, multipart=mime, headers=headers, impersonate="chrome", timeout=30)

    if resp.status_code != 201:
        print(f"Signaling failed: {resp.status_code}")
        await pc.close()
        return

    await pc.setRemoteDescription(RTCSessionDescription(sdp=resp.text.strip(), type="answer"))
    print("Signaling OK, waiting for connection...")

    await asyncio.wait_for(connection_ready.wait(), timeout=15)
    print(f"Connected! Sending 440Hz tone for 10 seconds...")

    # 发送 10 秒音频
    await asyncio.sleep(10)

    # 检查 RTP stats
    stats = await pc.getStats()
    for report in stats.values():
        if report.type == "outbound-rtp" and report.kind == "audio":
            print(f"\n=== Outbound Audio RTP ===")
            print(f"  packetsSent: {report.packetsSent}")
            print(f"  bytesSent: {report.bytesSent}")
            print(f"  expected packets (10s @ 50pps): ~500")

    print(f"\nRemote audio non-silence frames: {remote_non_silence}")
    print(f"{'✓ ChatGPT IS responding' if remote_non_silence > 50 else '✗ ChatGPT NOT responding'}")

    await pc.close()


if __name__ == "__main__":
    from services.account_service import account_service
    # 从 session 获取 token
    token = account_service.get_realtime_access_token()
    os.environ["CHATGPT_ACCESS_TOKEN"] = token
    asyncio.run(run())
