import asyncio
import json
import time

from av import AudioFrame, AudioResampler

from services.realtime.audio_track import BufferedAudioStreamTrack, FRAME_BYTES
from services.realtime.session import decode_data_channel_message, quota_error_from_message
from services.realtime.session import (
    RealtimeQuotaExceeded,
    RealtimeSession,
    resample_to_pcm16_mono,
)


def test_audio_track_paces_buffered_frames_in_realtime():
    async def run():
        track = BufferedAudioStreamTrack()
        track.push_pcm16(b"\x01\x00" * (FRAME_BYTES // 2 * 4))

        started = time.monotonic()
        frames = [await track.recv() for _ in range(4)]
        elapsed = time.monotonic() - started

        assert [frame.pts for frame in frames] == [0, 960, 1920, 2880]
        assert elapsed >= 0.05
        assert elapsed < 0.25

    asyncio.run(run())


def test_audio_track_clear_discards_queued_and_partial_audio():
    async def run():
        track = BufferedAudioStreamTrack()
        track.push_pcm16(b"\x01" * (FRAME_BYTES + 100))
        track.clear()

        frame = await track.recv()
        assert bytes(frame.planes[0]) == b"\x00" * FRAME_BYTES
        assert track._queue.empty()
        assert track._remainder == b""

    asyncio.run(run())


def test_audio_track_bounds_latency_by_dropping_oldest_frames():
    track = BufferedAudioStreamTrack(queue_max=3)
    track.push_pcm16(b"\x01\x00" * (FRAME_BYTES // 2 * 5))

    assert track.buffered_frames == 3
    assert track.dropped_frames == 2


def test_decode_data_channel_message_unwraps_chatgpt_envelope():
    inner = {
        "type": "state_update",
        "payload": {"previous_state": "idle", "new_state": "listening"},
    }
    raw = json.dumps({"type": "data_message", "data": json.dumps(inner)})

    assert decode_data_channel_message(raw) == inner


def test_quota_error_is_extracted_from_usage_update():
    message = {
        "type": "usage_update",
        "payload": {
            "rate_limit_message": {
                "exceed_limit_message": {
                    "title": "Daily Limit Reached",
                    "description_markdown": "You've reached your daily voice limit.",
                }
            }
        },
    }

    assert quota_error_from_message(message) == (
        "Daily Limit Reached: You've reached your daily voice limit."
    )


def test_quota_error_is_extracted_from_goodbye():
    message = {
        "type": "goodbye",
        "payload": {"reason": "cap_reached", "detail": "audio usage exceeded"},
    }

    assert quota_error_from_message(message) == "audio usage exceeded"


def test_realtime_session_rotates_account_after_quota_error():
    class StubSession(RealtimeSession):
        def __init__(self):
            super().__init__(
                identity={},
                model="test",
                websocket=object(),
                access_token="exhausted-token",
                access_token_provider=lambda excluded: "healthy-token",
            )
            self.attempts = []

        async def _start_once(self, access_token: str) -> None:
            self.attempts.append(access_token)
            if access_token == "exhausted-token":
                raise RealtimeQuotaExceeded("audio usage exceeded")

    async def run():
        session = StubSession()
        await session._start()
        assert session.attempts == ["exhausted-token", "healthy-token"]
        assert session._access_token == "healthy-token"

    asyncio.run(run())


def test_data_channel_queue_is_bounded():
    session = RealtimeSession(
        identity={},
        model="test",
        websocket=object(),
        access_token="token",
    )
    for index in range(600):
        session._queue_dc_message(str(index))

    assert session._dc_messages.qsize() == 512
    assert session._dc_dropped_messages == 88


def test_data_channel_queue_preserves_terminal_message_over_telemetry():
    session = RealtimeSession(
        identity={},
        model="test",
        websocket=object(),
        access_token="token",
    )
    telemetry = json.dumps({"type": "state_update", "payload": {"new_state": "listening"}})
    for _ in range(512):
        session._queue_dc_message(telemetry)

    terminal = json.dumps({"type": "goodbye", "payload": {"reason": "cap_reached"}})
    session._queue_dc_message(terminal)

    assert session._dc_messages.qsize() == 512
    assert terminal in session._dc_messages._queue


def test_remote_stereo_audio_is_downmixed_to_exact_mono_pcm_size():
    frame = AudioFrame(format="s16", layout="stereo", samples=960)
    frame.sample_rate = 48000
    # 同相左右声道，正确下混后仍应是可听的非零信号。
    frame.planes[0].update((b"\xe8\x03\xe8\x03") * 960)
    resampler = AudioResampler(format="s16", layout="mono", rate=48000)

    converted = resample_to_pcm16_mono(resampler, frame)

    assert len(converted) == 1
    output_frame, pcm = converted[0]
    assert output_frame.format.name == "s16"
    assert output_frame.layout.name == "mono"
    assert output_frame.sample_rate == 48000
    assert output_frame.samples == 960
    assert len(pcm) == 960 * 2
    assert pcm[:2] != b"\x00\x00"
