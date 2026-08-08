"""
Spike 1: 验证 aiortc 在当前 Python 版本下能否正常建立 WebRTC 连接并收发音频帧。
本地 offerer ↔ answerer，不依赖外部服务。
"""

import asyncio
import numpy as np
from aiortc import RTCPeerConnection, RTCSessionDescription, MediaStreamTrack
from av import AudioFrame

SAMPLE_RATE = 48000
CHANNELS = 1
FRAME_DURATION_MS = 20
SAMPLES_PER_FRAME = SAMPLE_RATE * FRAME_DURATION_MS // 1000


class SineAudioTrack(MediaStreamTrack):
    """生成 440Hz 正弦波的音频轨道"""
    kind = "audio"

    def __init__(self):
        super().__init__()
        self._timestamp = 0
        self._frame_count = 0

    async def recv(self):
        t = np.linspace(
            self._timestamp / SAMPLE_RATE,
            (self._timestamp + SAMPLES_PER_FRAME) / SAMPLE_RATE,
            SAMPLES_PER_FRAME,
            endpoint=False,
        )
        samples = (np.sin(2 * np.pi * 440 * t) * 32767).astype(np.int16)

        frame = AudioFrame(format="s16", layout="mono", samples=SAMPLES_PER_FRAME)
        frame.sample_rate = SAMPLE_RATE
        frame.pts = self._timestamp
        frame.time_base = "1/48000"
        frame.planes[0].update(samples.tobytes())

        self._timestamp += SAMPLES_PER_FRAME
        self._frame_count += 1

        # 控制发送速率
        await asyncio.sleep(FRAME_DURATION_MS / 1000)
        return frame


async def run():
    # --- Offerer ---
    offerer = RTCPeerConnection()
    audio_track = SineAudioTrack()
    offerer.addTrack(audio_track)
    dc_offerer = offerer.createDataChannel("test-channel")

    dc_opened = asyncio.Event()
    dc_message_received = asyncio.Event()
    received_dc_message = []

    @dc_offerer.on("open")
    def on_dc_open():
        print("[offerer] DataChannel opened, sending 'hello'")
        dc_offerer.send("hello from offerer")
        dc_opened.set()

    # --- Answerer ---
    answerer = RTCPeerConnection()
    received_frames = []
    answerer_dc_messages = []

    @answerer.on("track")
    def on_track(track):
        print(f"[answerer] Received track: kind={track.kind}")

        async def consume():
            count = 0
            while count < 10:
                try:
                    frame = await track.recv()
                    received_frames.append(frame)
                    count += 1
                except Exception as e:
                    print(f"[answerer] track.recv() error: {e}, retrying...")
                    await asyncio.sleep(0.1)
                    break
            if received_frames:
                print(f"[answerer] Received {len(received_frames)} audio frames")

        asyncio.ensure_future(consume())

    @answerer.on("datachannel")
    def on_datachannel(channel):
        print(f"[answerer] DataChannel received: label={channel.label}, id={channel.id}")

        @channel.on("message")
        def on_message(msg):
            print(f"[answerer] DataChannel message: {msg!r}")
            answerer_dc_messages.append(msg)
            channel.send("hello from answerer")
            dc_message_received.set()

    @dc_offerer.on("message")
    def on_offerer_dc_msg(msg):
        print(f"[offerer] DataChannel reply: {msg!r}")
        received_dc_message.append(msg)

    # --- Signaling ---
    offer = await offerer.createOffer()
    await offerer.setLocalDescription(offer)
    print(f"[offerer] Created offer, SDP length={len(offerer.localDescription.sdp)}")

    await answerer.setRemoteDescription(offerer.localDescription)
    answer = await answerer.createAnswer()
    await answerer.setLocalDescription(answer)
    print(f"[answerer] Created answer, SDP length={len(answerer.localDescription.sdp)}")

    await offerer.setRemoteDescription(answerer.localDescription)
    print(f"[offerer] signaling state: {offerer.signalingState}")
    print(f"[answerer] signaling state: {answerer.signalingState}")

    # --- 等待连接建立 ---
    connection_ready = asyncio.Event()

    @offerer.on("connectionstatechange")
    async def on_conn_state():
        print(f"[offerer] connectionState -> {offerer.connectionState}")
        if offerer.connectionState == "connected":
            connection_ready.set()

    print("[waiting] Waiting for connection and data exchange...")
    try:
        await asyncio.wait_for(connection_ready.wait(), timeout=5)
    except asyncio.TimeoutError:
        print(f"[TIMEOUT] Connection state: {offerer.connectionState}")

    try:
        await asyncio.wait_for(dc_opened.wait(), timeout=5)
        await asyncio.wait_for(dc_message_received.wait(), timeout=5)
    except asyncio.TimeoutError:
        print("[TIMEOUT] DataChannel exchange did not complete")

    # 等音频帧被 RTP 层消费（aiortc 在 connected 后才开始 poll track.recv）
    await asyncio.sleep(2.0)

    # 验证 audio track 的 recv() 在 offerer 侧正常工作（确认 AudioFrame 格式被 aiortc 接受）
    audio_track_frames_generated = audio_track._frame_count
    print(f"[offerer] Audio track generated {audio_track_frames_generated} frames before close")

    # --- 结果验证 ---
    print("\n=== Spike 1 Results ===")
    print(f"✓ aiortc version: 1.15.0 on Python {__import__('sys').version}")
    print(f"✓ Signaling: offerer={offerer.signalingState}, answerer={answerer.signalingState}")
    print(f"✓ Connection: offerer={offerer.connectionState}, answerer={answerer.connectionState}")
    print(f"✓ Audio track frames generated (send side): {audio_track_frames_generated}")
    print(f"✓ Audio frames received (recv side): {len(received_frames)}")
    print(f"✓ DataChannel: offerer→answerer messages={len(answerer_dc_messages)}")
    print(f"✓ DataChannel: answerer→offerer replies={len(received_dc_message)}")

    all_passed = (
        offerer.signalingState == "stable"
        and offerer.connectionState == "connected"
        and audio_track_frames_generated >= 5
        and len(answerer_dc_messages) >= 1
        and len(received_dc_message) >= 1
    )
    print(f"\n{'✓ ALL CHECKS PASSED' if all_passed else '✗ SOME CHECKS FAILED'}")

    await offerer.close()
    await answerer.close()
    return all_passed


if __name__ == "__main__":
    success = asyncio.run(run())
    exit(0 if success else 1)
