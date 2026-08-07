from __future__ import annotations

import asyncio
from fractions import Fraction

from aiortc import MediaStreamTrack
from av import AudioFrame

SAMPLE_RATE = 48000
SAMPLES_PER_FRAME = 960  # 20ms at 48kHz
FRAME_BYTES = SAMPLES_PER_FRAME * 2  # s16 mono = 1920 bytes
FRAME_DURATION = SAMPLES_PER_FRAME / SAMPLE_RATE
SILENCE = b"\x00" * FRAME_BYTES


class BufferedAudioStreamTrack(MediaStreamTrack):
    """从外部推送 PCM16 数据的音频轨道。

    关键设计：以固定 20ms 间隔发送帧，保持 RTP 时钟连续。
    有数据时发真实音频，无数据时发静音但保持节奏。
    """

    kind = "audio"

    def __init__(self, queue_max: int = 300):
        super().__init__()
        self._queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=queue_max)
        self._pts = 0
        self._remainder = b""
        self._next_frame_at: float | None = None

    def push_pcm16(self, data: bytes) -> None:
        """推入任意长度的 PCM16 mono 48kHz 数据，内部按 960 samples 切帧。"""
        buf = self._remainder + data
        offset = 0
        while offset + FRAME_BYTES <= len(buf):
            chunk = buf[offset:offset + FRAME_BYTES]
            if self._queue.full():
                try:
                    self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                self._queue.put_nowait(chunk)
            except asyncio.QueueFull:
                pass
            offset += FRAME_BYTES
        self._remainder = buf[offset:]

    def clear(self) -> None:
        """丢弃尚未发送的音频，包括不足一帧的尾部数据。"""
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        self._remainder = b""

    async def recv(self) -> AudioFrame:
        # aiortc 会尽可能快地调用 recv()，轨道自身必须负责节拍。只在队列为空
        # 时 sleep 会让积压的语音瞬间排空，造成 RTP 时间戳与真实发送时间不一致，
        # 上游 VAD 因而无法把它识别成连续的实时语音。
        loop = asyncio.get_running_loop()
        now = loop.time()
        if self._next_frame_at is None:
            self._next_frame_at = now
        else:
            # 事件循环曾经卡顿时从“现在”重新起拍，避免随后突发补发多帧。
            self._next_frame_at = max(self._next_frame_at + FRAME_DURATION, now)
            await asyncio.sleep(max(0.0, self._next_frame_at - now))

        try:
            pcm = self._queue.get_nowait()
        except asyncio.QueueEmpty:
            pcm = SILENCE

        frame = AudioFrame(format="s16", layout="mono", samples=SAMPLES_PER_FRAME)
        frame.sample_rate = SAMPLE_RATE
        frame.pts = self._pts
        frame.time_base = Fraction(1, SAMPLE_RATE)
        frame.planes[0].update(pcm)
        self._pts += SAMPLES_PER_FRAME
        return frame
