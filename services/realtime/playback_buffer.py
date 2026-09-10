from __future__ import annotations

import array
import random
import sys
from dataclasses import dataclass

from services.realtime.audio_track import SAMPLE_RATE
from services.realtime.session import PcmOutputAssembler

QUANTUM = 128
DEFAULT_PREFILL_SECONDS = 0.42
DEFAULT_MAX_PREFILL_SECONDS = 0.84
DEFAULT_MAX_SECONDS = 4.0


class PlaybackJitterBuffer:
    """客户端播放抖动缓冲的参考实现。

    未凑够预填时只出静音；开播后严格按 1.0x 取数。短缺口保持开播并出
    静音，水位过高时丢掉最旧样本，而不是加快语速。
    """

    def __init__(
        self,
        sample_rate: int = SAMPLE_RATE,
        prefill_seconds: float = DEFAULT_PREFILL_SECONDS,
        max_prefill_seconds: float = DEFAULT_MAX_PREFILL_SECONDS,
        max_seconds: float = DEFAULT_MAX_SECONDS,
    ):
        self.sample_rate = sample_rate
        self.prefill = max(1, int(sample_rate * prefill_seconds))
        self.max_prefill = max(self.prefill, int(sample_rate * max_prefill_seconds))
        self.max_samples = max(self.max_prefill, int(sample_rate * max_seconds))
        self._samples = array.array("h")
        self.playing = False
        self.ending = False
        self.played = 0
        self.underrun_samples = 0
        self.rebuffer_events = 0
        self.dropped_incoming = 0
        self.rate_adjusts = 0
        self._hunger = 0
        self._hunger_limit = int(sample_rate * 1.2)

    @property
    def available(self) -> int:
        return len(self._samples)

    def push(self, pcm: bytes) -> None:
        incoming = array.array("h")
        length = len(pcm) - (len(pcm) % 2)
        if length < 2:
            return
        incoming.frombytes(pcm[:length])
        if sys.byteorder != "little":
            incoming.byteswap()
        overflow = len(self._samples) + len(incoming) - self.max_samples
        if overflow > 0:
            self.dropped_incoming += overflow
            if overflow >= len(self._samples):
                incoming = incoming[overflow - len(self._samples):]
                self._samples = array.array("h")
            else:
                del self._samples[:overflow]
        self._samples.extend(incoming)
        if self.ending:
            self.ending = False
        if not self.playing and len(self._samples) >= self.prefill:
            self.playing = True
        elif not self.playing and self._hunger > 0 and self._samples:
            self.playing = True

    def end(self) -> None:
        self.ending = True
        if self._samples:
            self.playing = True

    def stop(self) -> None:
        self._samples = array.array("h")
        self.playing = False
        self.ending = False
        self._hunger = 0

    def pull(self, n: int) -> array.array:
        out = array.array("h", [0] * n)
        if not self.playing:
            if self._samples and (self.ending or len(self._samples) >= self.prefill or self._hunger > 0):
                self.playing = True
            else:
                return out

        got = min(n, len(self._samples))
        if got:
            out[:got] = self._samples[:got]
            del self._samples[:got]
            self._hunger = 0

        if got == 0:
            if self.ending:
                self.playing = False
                self.ending = False
                return out
            self.underrun_samples += n
            self._hunger += n
            if self._hunger >= self._hunger_limit:
                self.playing = False
                self.prefill = min(self.max_prefill, self.prefill + n * 8)
                self._hunger = 0
                self.rebuffer_events += 1
            self.played += n
            return out

        if got < n and not self.ending:
            self.underrun_samples += n - got

        self.played += n
        return out


@dataclass(frozen=True)
class PlayoutReport:
    played: int
    underrun_samples: int
    rebuffer_events: int
    done_events: int
    dropped_incoming: int
    rate_adjusts: int = 0


def simulate_ws_playout(
    pcm: bytes,
    *,
    frame_samples: int = 960,
    assembler: PcmOutputAssembler | None = None,
    buffer: PlaybackJitterBuffer | None = None,
    jitter_max_s: float = 0.08,
    spike_every: int = 12,
    spike_s: float = 0.12,
    consume_rate: float = 1.0,
    seed: int = 1,
) -> PlayoutReport:
    """按 20ms 帧 → 协议分片 → 带抖动到达 → 128 样本量取数，统计中途欠载。"""
    assembler = assembler or PcmOutputAssembler()
    buffer = buffer or PlaybackJitterBuffer()
    rng = random.Random(seed)
    frame_bytes = frame_samples * 2
    arrivals: list[tuple[float, bytes | None]] = []
    generated_s = 0.0
    chunk_index = 0

    def enqueue(events, generated_at: float) -> None:
        nonlocal chunk_index
        saw_done = False
        for event in events:
            if event.kind == "delta":
                delay = rng.random() * jitter_max_s
                if spike_every and chunk_index % spike_every == 0:
                    delay += spike_s
                arrivals.append((generated_at + delay, event.pcm))
                chunk_index += 1
            else:
                saw_done = True
        if saw_done:
            last_at = max((at for at, _ in arrivals), default=generated_at)
            arrivals.append((last_at, None))

    offset = 0
    while offset < len(pcm):
        frame = pcm[offset:offset + frame_bytes]
        if len(frame) < frame_bytes:
            frame = frame + b"\x00" * (frame_bytes - len(frame))
        offset += frame_bytes
        generated_s += frame_samples / SAMPLE_RATE
        enqueue(assembler.push(frame), generated_s)
    enqueue(assembler.finish(), generated_s)
    arrivals.sort(key=lambda item: item[0])

    now = 0.0
    cursor = 0
    quantum_s = QUANTUM / (SAMPLE_RATE * consume_rate)
    deadline = (arrivals[-1][0] if arrivals else 0.0) + buffer.prefill / buffer.sample_rate + 2.0
    limit = max(deadline + 1.0, generated_s / consume_rate + 8.0)
    while now <= deadline or cursor < len(arrivals) or buffer.available or buffer.playing:
        while cursor < len(arrivals) and arrivals[cursor][0] <= now:
            payload = arrivals[cursor][1]
            if payload is None:
                buffer.end()
            else:
                buffer.push(payload)
            cursor += 1
        buffer.pull(QUANTUM)
        now += quantum_s
        if now > limit:
            break

    return PlayoutReport(
        played=buffer.played,
        underrun_samples=buffer.underrun_samples,
        rebuffer_events=buffer.rebuffer_events,
        done_events=sum(1 for _, payload in arrivals if payload is None),
        dropped_incoming=buffer.dropped_incoming,
        rate_adjusts=buffer.rate_adjusts,
    )
