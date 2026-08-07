from __future__ import annotations

import asyncio
from fractions import Fraction

from aiortc import MediaStreamTrack
from av import AudioFrame

SAMPLE_RATE = 48000
SAMPLES_PER_FRAME = 960  # 20ms at 48kHz
FRAME_BYTES = SAMPLES_PER_FRAME * 2  # s16 mono


class BufferedAudioStreamTrack(MediaStreamTrack):
    """从外部推送 PCM16 数据的音频轨道。aiortc RTP 层在发送时自动 Opus 编码。"""

    kind = "audio"

    def __init__(self, queue_max: int = 200):
        super().__init__()
        self._queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=queue_max)
        self._pts = 0

    def push_pcm16(self, data: bytes) -> None:
        """推入 PCM16 mono 48kHz 数据（每次 960 samples = 1920 bytes）。
        如果队列满则丢弃最旧帧（防止积压）。
        """
        offset = 0
        while offset + FRAME_BYTES <= len(data):
            chunk = data[offset:offset + FRAME_BYTES]
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

    async def recv(self) -> AudioFrame:
        try:
            pcm = await asyncio.wait_for(self._queue.get(), timeout=0.04)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pcm = b"\x00" * FRAME_BYTES

        frame = AudioFrame(format="s16", layout="mono", samples=SAMPLES_PER_FRAME)
        frame.sample_rate = SAMPLE_RATE
        frame.pts = self._pts
        frame.time_base = Fraction(1, SAMPLE_RATE)
        frame.planes[0].update(pcm)
        self._pts += SAMPLES_PER_FRAME
        return frame
