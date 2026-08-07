import asyncio
import json
import time

from services.realtime.audio_track import BufferedAudioStreamTrack, FRAME_BYTES
from services.realtime.session import decode_data_channel_message, quota_error_from_message
from services.realtime.session import RealtimeQuotaExceeded, RealtimeSession


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
