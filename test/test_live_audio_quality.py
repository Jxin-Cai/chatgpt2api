"""Audio continuity regressions: exercise the shipped RTP track, not a simulator."""
import asyncio
from types import SimpleNamespace
from fractions import Fraction

import numpy as np
from av import AudioFrame

import services.realtime.audio_track as audio


def run_track(monkeypatch, run):
    clock = SimpleNamespace(now=0.0)
    async def sleep(delay):
        clock.now += delay
    monkeypatch.setattr(audio, "asyncio", SimpleNamespace(
        Queue=asyncio.Queue, QueueEmpty=asyncio.QueueEmpty, QueueFull=asyncio.QueueFull,
        get_running_loop=lambda: SimpleNamespace(time=lambda: clock.now), sleep=sleep,
    ))
    return asyncio.run(run(clock))


def block(value=12000, frames=1):
    return np.full(960 * frames, value, dtype="<i2").tobytes()


def test_rtp_output_absorbs_25ms_arrival_jitter_without_inserting_silence(monkeypatch):
    async def run(clock):
        from services.live.transport import WebRTCTransport
        transport = WebRTCTransport()
        track = transport.output
        arrivals = [(i * .04 + (.025 if i % 8 == 4 else 0)) for i in range(50)]
        result = []
        try:
            for tick in range(110):
                clock.now = tick * .02
                while arrivals and arrivals[0] <= clock.now + 1e-6:
                    arrivals.pop(0)
                    track.push_pcm16(block(frames=2))
                result.append((await track.recv()).to_ndarray().ravel())
            samples = np.concatenate(result)
            active = np.flatnonzero(samples)
            assert len(active)
            assert not np.any(samples[active[0]:active[-1]] == 0), "Jitter inserted hard silence into continuous audio"
        finally:
            await transport.close()
    run_track(monkeypatch, run)


def test_rtp_output_smooths_underrun_restart_and_clear(monkeypatch):
    async def run(clock):
        from services.live.transport import WebRTCTransport
        transport = WebRTCTransport()
        track = transport.output
        rendered = []
        try:
            track.push_pcm16(block(frames=4))
            rendered.extend([(await track.recv()).to_ndarray().ravel() for _ in range(9)])
            track.push_pcm16(block(-12000, frames=4))
            rendered.extend([(await track.recv()).to_ndarray().ravel() for _ in range(5)])
            track.clear()
            rendered.append((await track.recv()).to_ndarray().ravel())
            samples = np.concatenate(rendered).astype(np.int32)
            assert np.max(np.abs(np.diff(samples))) <= 100, "Hard audio/silence transitions produce clicks"
            assert samples[-1] == 0
        finally:
            await transport.close()
    run_track(monkeypatch, run)


def test_rtp_output_releases_short_tail_and_crossfades_overflow(monkeypatch):
    async def run(clock):
        track = audio.BufferedAudioStreamTrack(queue_max=3, prefill_frames=3, smooth_edges=True)
        track.push_pcm16(block())
        frames = [(await track.recv()).to_ndarray().ravel() for _ in range(3)]
        assert frames[-1][-1] == 12000, "A short utterance must not wait forever for prefill"
        track.push_pcm16(block(-12000, frames=5))
        following = (await track.recv()).to_ndarray().ravel()
        assert track.dropped_frames == 2
        assert following[0] == 12000
        assert following[-1] == -12000
        assert np.max(np.abs(np.diff(following.astype(np.int32)))) <= 101
    run_track(monkeypatch, run)


def test_webrtc_forwards_quiet_frames_without_replaying_dtx_gap():
    from services.live.protocol import parse_config
    from services.live.session import LiveSession
    async def run():
        frames = []
        for pts, value in [(0, 3000), (960, 0), (48000, 50)]:
            frame = AudioFrame(format="s16", layout="mono", samples=960)
            frame.sample_rate = 48000
            frame.time_base = Fraction(1, 48000)
            frame.pts = pts
            frame.planes[0].update(block(value))
            frames.append(frame)
        class Source:
            async def recv(self):
                if not frames:
                    raise RuntimeError("test stream ended")
                return frames.pop(0)
        class Sink:
            def __init__(self): self.pcm = []
            def write_audio(self, pcm): self.pcm.append(pcm)
            async def send_text(self, text): raise AssertionError("WebRTC media must not pass through JSON output queues")
        sink = Sink()
        session = LiveSession(identity={}, websocket=sink, access_token="dummy", config=parse_config({"model": "gpt-live-1"}, "webrtc"))
        session._remote_audio_track = Source()
        await session._audio_sender()
        assert b"".join(sink.pcm) == block(3000) + block(0) + block(50)
    asyncio.run(run())


def test_discards_startup_backlog_when_browser_transport_is_not_ready():
    from services.live.transport import WebRTCTransport

    async def run():
        # Arrange: provider frames arrive before the browser finishes ICE.
        transport = WebRTCTransport()
        try:
            # Act
            for _ in range(200):
                transport.write_audio(block())
            transport.channel = SimpleNamespace(readyState="open")
            transport.write_audio(block(-12000))
            # Assert: only fresh media can enter playout; no overflow splice.
            assert transport.output.buffered_frames == 1
            assert transport.output.dropped_frames == 0
        finally:
            await transport.close()
    asyncio.run(run())


def test_drains_upstream_before_browser_ready_when_webrtc_is_prepared():
    from unittest.mock import AsyncMock
    from services.live.protocol import parse_config
    from services.live.session import LiveSession

    async def run():
        # Arrange
        session = LiveSession(identity={}, websocket=object(), access_token="dummy",
                              config=parse_config({"model": "gpt-live-1"}, "webrtc"))
        session._start = AsyncMock()
        draining = asyncio.Event()
        async def drain():
            draining.set()
            await asyncio.Event().wait()
        session._audio_sender = drain
        # Act
        await session.prepare()
        await asyncio.wait_for(draining.wait(), 1)
        task = session._media_task
        await session.close()
        # Assert: preparation owns the reader, including failed-handshake cleanup.
        assert task.cancelled()
    asyncio.run(run())


def test_account_update_does_not_pause_active_media_loop():
    import threading
    from unittest.mock import AsyncMock
    from services.live.protocol import parse_config
    from services.live.session import LiveSession

    async def run():
        release = threading.Event()
        serviced_during_update = []
        def update_account(_token):
            # Models an account refresh / disk write waiting on I/O. The media
            # event loop must remain able to deliver frames while it waits.
            serviced_during_update.append(release.wait(.5))
        session = LiveSession(identity={}, websocket=object(), access_token="dummy",
                              config=parse_config({"model": "gpt-live-1"}),
                              account_available_callback=update_account)
        session._start_once = AsyncMock()
        async def service_media():
            await asyncio.sleep(.01)
            release.set()
        await asyncio.gather(session._start(), service_media())
        assert serviced_during_update == [True]
    asyncio.run(run())
