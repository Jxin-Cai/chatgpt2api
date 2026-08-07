from __future__ import annotations

import asyncio
from fractions import Fraction

from aiortc import MediaStreamTrack
from av import AudioFrame

SAMPLE_RATE = 48000
SAMPLES_PER_FRAME = 960  # 20ms at 48kHz
FRAME_BYTES = SAMPLES_PER_FRAME * 2  # s16 mono = 1920 bytes


class BufferedAudioStreamTrack(MediaStreamTrack):
    """从外部推送 PCM16 数据的音频轨道。aiortc RTP 层在发送时自动 Opus 编码。"""

    kind = "audio"

    def __init__(self, queue_max: int = 200):
        super().__init__()
        self._queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=queue_max)
        self._pts = 0
        self._remainder = b""
        self._recv_count = 0
        self._real_data_count = 0

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

    async def recv(self) -> AudioFrame:
        try:
            pcm = await asyncio.wait_for(self._queue.get(), timeout=0.02)
            self._real_data_count += 1
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pcm = b"\x00" * FRAME_BYTES

        self._recv_count += 1
        if self._recv_count <= 5 or self._recv_count % 500 == 0:
            import logging
            logging.getLogger("uvicorn").info(
                f"[audio_track] recv #{self._recv_count}: real_data={self._real_data_count}, "
                f"queue={self._queue.qsize()}, ratio={self._real_data_count/max(1,self._recv_count)*100:.1f}%"
            )

        frame = AudioFrame(format="s16", layout="mono", samples=SAMPLES_PER_FRAME)
        frame.sample_rate = SAMPLE_RATE
        frame.pts = self._pts
        frame.time_base = Fraction(1, SAMPLE_RATE)
        frame.planes[0].update(pcm)
        self._pts += SAMPLES_PER_FRAME
        return frame
