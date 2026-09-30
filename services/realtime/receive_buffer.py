"""Bounded reordering for packetized audio on the two server WebRTC legs.

aiortc 1.15's generic jitter buffer waits for contiguous frames. One missing
audio packet can fill its 16-packet ring and leave all later speech ~300ms
behind. Opus / G.711 RTP payloads are complete audio frames: release them after
a short reorder window, skipping missing packets instead of retaining the lag.
"""
from __future__ import annotations

from aiortc.jitterbuffer import JitterFrame
from aiortc.rtp import RtpPacket


class AudioReceiveBuffer:
    def __init__(self, prefetch: int = 3):
        if not 1 <= prefetch <= 16:
            raise ValueError("Audio prefetch must be between 1 and 16 packets")
        self.prefetch = prefetch
        self._origin: int | None = None
        self._latest: int | None = None
        self._ssrc: int | None = None
        self._packets: dict[int, RtpPacket] = {}
        self.skipped_packets = 0
        self.late_packets = 0
        self.max_pending = 0

    def add(self, packet: RtpPacket) -> tuple[bool, JitterFrame | None]:
        seq = packet.sequence_number
        if self._origin is None or packet.ssrc != self._ssrc:
            self._packets.clear()
            self._origin = self._latest = seq
            self._ssrc = packet.ssrc
        distance = (seq - self._origin) & 0xFFFF
        if distance >= 0x8000:
            self.late_packets += 1
            return False, None
        if distance > self.prefetch * 2:
            # A long outage must not replay the old queue on reconnection.
            self.skipped_packets += distance - self.prefetch
            self._origin = (seq - self.prefetch) & 0xFFFF
            self._packets = {key: value for key, value in self._packets.items()
                             if ((key - self._origin) & 0xFFFF) <= self.prefetch}
        if 0 < ((seq - self._latest) & 0xFFFF) < 0x8000:
            self._latest = seq
        self._packets.setdefault(seq, packet)
        self.max_pending = max(self.max_pending, len(self._packets))
        while ((self._latest - self._origin) & 0xFFFF) >= self.prefetch:
            ready = self._packets.pop(self._origin, None)
            self._origin = (self._origin + 1) & 0xFFFF
            if ready is not None:
                return False, JitterFrame(data=ready._data, timestamp=ready.timestamp)
            self.skipped_packets += 1
        return False, None


def configure_audio_receivers(pc) -> list[AudioReceiveBuffer]:
    """Install before receiving RTP; never modify video or global aiortc state.

    aiortc has no public receiver buffer hook. Keep the private integration in
    one guarded location, covered by the real local WebRTC integration test.
    """
    buffers = []
    for transceiver in pc.getTransceivers():
        if transceiver.kind != "audio":
            continue
        receiver = transceiver.receiver
        attr = "_RTCRtpReceiver__jitter_buffer"
        if not hasattr(receiver, attr):
            raise RuntimeError("Unsupported aiortc audio receiver buffer API")
        current = getattr(receiver, attr)
        if not isinstance(current, AudioReceiveBuffer):
            current = AudioReceiveBuffer()
            setattr(receiver, attr, current)
        buffers.append(current)
    return buffers


def audio_receive_stats(pc) -> dict[str, int]:
    buffers = [getattr(t.receiver, "_RTCRtpReceiver__jitter_buffer", None)
               for t in getattr(pc, "getTransceivers", lambda: [])() if t.kind == "audio"]
    return {name: sum(getattr(b, name, 0) for b in buffers)
            for name in ("skipped_packets", "late_packets")}
