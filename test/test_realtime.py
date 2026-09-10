import asyncio
import base64
import json
import math
import struct
import time
from fractions import Fraction

import pytest
from av import AudioFrame, AudioResampler

import api.realtime as realtime_api
from services.realtime.audio_track import BufferedAudioStreamTrack, FRAME_BYTES
from services.realtime.chatgpt_webrtc import (
    OFFICIAL_VOICE_ALIASES,
    REALTIME_VOICE_IDS,
    build_session_config,
    official_aliases_for,
    resolve_voice,
)
from services.realtime.session import decode_data_channel_message, quota_error_from_message
from services.realtime.playback_buffer import (
    PlaybackJitterBuffer,
    simulate_ws_playout,
)
from services.realtime.session import (
    END_AUDIO_STATES,
    PcmOutputAssembler,
    RealtimeQuotaExceeded,
    RealtimeSession,
    pcm16_rms,
    resample_to_pcm16_mono,
    silence_to_cover_gap,
)
from services.realtime.signaling import RealtimeSignalingGuard


def test_build_session_config_omits_conversation_continuity_when_not_provided():
    config = build_session_config()

    assert "conversation_id" not in config
    assert "parent_message_id" not in config


def test_build_session_config_includes_conversation_continuity_when_provided():
    config = build_session_config(
        conversation_id="conversation-123",
        parent_message_id="parent-456",
    )

    assert config["conversation_id"] == "conversation-123"
    assert config["parent_message_id"] == "parent-456"


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


def test_realtime_session_forwards_relay_message_to_upstream_data_channel():
    class StubDataChannel:
        readyState = "open"

        def __init__(self):
            self.messages = []

        def send(self, message):
            self.messages.append(message)

    async def run():
        session = RealtimeSession(
            identity={},
            model="test",
            websocket=object(),
            access_token="token",
        )
        session._data_channel = StubDataChannel()
        event = {
            "type": "relay_message",
            "payload": {"type": "relay_message", "message": {"id": "message-1"}},
        }

        await session._handle_client_event("relay_message", event)

        assert len(session._data_channel.messages) == 1
        outer = json.loads(session._data_channel.messages[0])
        assert json.loads(outer["data"]) == event

    asyncio.run(run())


def test_resolve_voice_accepts_native_ids_and_official_aliases():
    assert resolve_voice("ember") == "ember"
    assert resolve_voice("MARIN") == "ember"
    assert resolve_voice("cedar") == "fathom"
    assert resolve_voice("") == "ember"
    assert resolve_voice(None) == "ember"
    assert resolve_voice("not-a-voice") is None
    # 每个官方声音名都必须映射到一个真实存在的上游声音。
    for alias, target in OFFICIAL_VOICE_ALIASES.items():
        assert target in REALTIME_VOICE_IDS, alias


def test_official_aliases_for_lists_reverse_mapping():
    assert "marin" in official_aliases_for("ember")
    assert official_aliases_for("fathom") == ["cedar"]


def test_parse_session_options_reads_official_ga_shape():
    options = realtime_api.parse_session_options({
        "type": "realtime",
        "model": "gpt-realtime",
        "audio": {
            "output": {"voice": "marin"},
            "input": {"transcription": {"language": "zh-CN"}},
        },
        "chatgpt2api": {
            "attempt_id": "a" * 32,
            "conversation_id": "conversation-123",
            "parent_message_id": "parent-456",
            "resume_handle": "handle-789",
        },
    })

    assert options["voice"] == "ember"
    assert options["voice_requested"] == "marin"
    assert options["language"] == "zh-CN"
    assert options["model_requested"] == "gpt-realtime"
    assert options["attempt_id"] == "a" * 32
    assert options["conversation_id"] == "conversation-123"
    assert options["parent_message_id"] == "parent-456"
    assert options["resume_handle"] == "handle-789"


def test_parse_session_options_rejects_unknown_voice_with_param():
    with pytest.raises(realtime_api.SessionConfigError) as exc_info:
        realtime_api.parse_session_options({"audio": {"output": {"voice": "nope"}}})

    assert exc_info.value.param == "session.audio.output.voice"


def test_parse_session_options_ignores_unknown_fields_and_empty_config():
    assert realtime_api.parse_session_options(None) == {}
    assert realtime_api.parse_session_options({"instructions": "hi", "tools": []}) == {}


def test_client_secret_mint_and_redeem_round_trip():
    guard = RealtimeSignalingGuard()
    secret, ttl = guard.mint_client_secret(
        "tester",
        {"id": "tester", "name": "tester"},
        session_options={"voice": "ember"},
        ttl_seconds=60,
    )

    assert secret.startswith("ek_")
    assert ttl == 60
    record = guard.redeem_client_secret(secret)
    assert record is not None
    assert record.identity_key == "tester"
    assert record.session_options == {"voice": "ember"}
    # 兑换不是一次性的：网络重试期间允许重复使用。
    assert guard.redeem_client_secret(secret) is not None
    assert guard.redeem_client_secret("ek_unknown") is None
    assert guard.redeem_client_secret("not-a-secret") is None


def test_client_secret_ttl_is_clamped_to_configured_maximum():
    guard = RealtimeSignalingGuard()
    _, ttl = guard.mint_client_secret(
        "tester",
        {},
        ttl_seconds=guard.client_secret_max_ttl_seconds + 999,
    )

    assert ttl == guard.client_secret_max_ttl_seconds


def _realtime_test_client(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    monkeypatch.setattr(
        realtime_api,
        "require_identity",
        lambda authorization: {"id": "tester", "name": "tester"},
    )

    async def fake_exchange(**kwargs):
        fake_exchange.calls.append(kwargs)
        return "v=0\r\no=answer\r\n", "/upstream/location"

    fake_exchange.calls = []
    monkeypatch.setattr(realtime_api, "exchange_realtime_sdp", fake_exchange)
    monkeypatch.setattr(
        realtime_api.account_service,
        "get_realtime_access_token",
        lambda excluded=None: "upstream-token",
    )
    monkeypatch.setattr(
        realtime_api.account_service, "mark_realtime_available", lambda token: None
    )

    app = FastAPI()
    app.include_router(realtime_api.create_router())
    return TestClient(app), fake_exchange


def test_client_secrets_endpoint_returns_openai_ga_shape(monkeypatch):
    client, _ = _realtime_test_client(monkeypatch)

    response = client.post(
        "/v1/realtime/client_secrets",
        headers={"Authorization": "Bearer test-key"},
        json={
            "expires_after": {"anchor": "created_at", "seconds": 120},
            "session": {
                "type": "realtime",
                "model": "gpt-realtime",
                "audio": {"output": {"voice": "marin"}},
            },
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["value"].startswith("ek_")
    assert payload["expires_at"] > time.time()
    assert payload["session"]["audio"]["output"]["voice"] == "marin"


def test_calls_endpoint_exchanges_raw_sdp_with_ephemeral_key(monkeypatch):
    client, fake_exchange = _realtime_test_client(monkeypatch)

    secret = client.post(
        "/v1/realtime/client_secrets",
        headers={"Authorization": "Bearer test-key"},
        json={"session": {"audio": {"output": {"voice": "cedar"}}}},
    ).json()["value"]

    offer_sdp = "v=0\r\n" + "a=fake\r\n" * 30
    response = client.post(
        "/v1/realtime/calls",
        headers={
            "Authorization": f"Bearer {secret}",
            "Content-Type": "application/sdp",
        },
        content=offer_sdp,
    )

    assert response.status_code == 201
    assert response.headers["content-type"].startswith("application/sdp")
    assert response.text == "v=0\r\no=answer\r\n"
    call_id = response.headers["Location"].rsplit("/", 1)[-1]
    assert call_id == response.headers["X-Attempt-Id"]
    assert response.headers["X-Session-Handle"]
    # ephemeral key 中登记的官方声音名应被换算为上游声音 id。
    assert fake_exchange.calls[-1]["voice"] == "fathom"


def test_calls_endpoint_accepts_multipart_unified_interface(monkeypatch):
    client, fake_exchange = _realtime_test_client(monkeypatch)

    offer_sdp = "v=0\r\n" + "a=fake\r\n" * 30
    # 官方 unified interface 形状：multipart FormData（sdp + session JSON）。
    response = client.post(
        "/v1/realtime/calls",
        headers={"Authorization": "Bearer test-key"},
        data={"session": json.dumps({"audio": {"output": {"voice": "sage"}}})},
        files={"sdp": (None, offer_sdp)},
    )

    assert response.status_code == 201
    assert fake_exchange.calls[-1]["voice"] == "vale"

    # 表单编码同样宽松接受。
    urlencoded = client.post(
        "/v1/realtime/calls",
        headers={"Authorization": "Bearer test-key"},
        data={
            "sdp": offer_sdp,
            "session": json.dumps({"audio": {"output": {"voice": "echo"}}}),
        },
    )

    assert urlencoded.status_code == 201
    assert fake_exchange.calls[-1]["voice"] == "orbit"


def test_calls_endpoint_rejects_missing_sdp(monkeypatch):
    client, _ = _realtime_test_client(monkeypatch)

    response = client.post(
        "/v1/realtime/calls",
        headers={
            "Authorization": "Bearer test-key",
            "Content-Type": "application/sdp",
        },
        content="too-short",
    )

    assert response.status_code == 400
    assert response.json()["error"]["param"] == "sdp"


def test_legacy_sessions_endpoint_accepts_official_voice_alias(monkeypatch):
    client, fake_exchange = _realtime_test_client(monkeypatch)

    response = client.post(
        "/v1/realtime/sessions",
        headers={"Authorization": "Bearer test-key"},
        json={"sdp": "v=0\r\n" + "a=fake\r\n" * 30, "voice": "marin"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["sdp"] == "v=0\r\no=answer\r\n"
    assert payload["attempt_id"]
    assert payload["session_handle"] == payload["resume_handle"]
    assert fake_exchange.calls[-1]["voice"] == "ember"


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


def test_pcm16_rms_detects_silence_and_speech():
    assert pcm16_rms(b"\x00\x00" * 32) == 0
    assert pcm16_rms(struct.pack("<h", 1000) * 32) > 900


def test_pcm_output_assembler_skips_idle_silence_and_keeps_speech_contiguous():
    assembler = PcmOutputAssembler(chunk_bytes=8, max_silence_bytes=16, silence_rms=100)
    loud = struct.pack("<4h", 1200, 1200, 1200, 1200)
    quiet = struct.pack("<4h", 0, 0, 0, 0)

    assert assembler.push(quiet) == []
    first = assembler.push(loud)
    second = assembler.push(loud)
    pause = assembler.push(quiet)
    held = assembler.push(quiet)

    assert [event.kind for event in first] == ["delta"]
    assert first[0].pcm == loud
    assert second[0].pcm == loud
    assert [event.kind for event in held] == ["delta"]
    assert not any(event.kind == "done" for event in [*first, *second, *pause, *held])
    finished = assembler.finish()
    assert [event.kind for event in finished] == ["done"]
    reconstructed = b"".join(
        event.pcm for event in [*first, *second, *pause, *held] if event.kind == "delta"
    )
    assert reconstructed == loud + loud + quiet + quiet


def test_ws_writer_sends_audio_before_transcript_backlog():
    class FakeWs:
        def __init__(self):
            self.sent: list[str] = []

        async def send_text(self, payload: str) -> None:
            self.sent.append(payload)

    async def run():
        session = RealtimeSession(
            identity={},
            model="test",
            websocket=FakeWs(),
            access_token="token",
        )
        session._writer_started = True
        for index in range(8):
            await session._send_event("chat_message_delta", {"n": index})
        await session._send_event("response.audio.delta", {"delta": "abc"})
        writer = asyncio.create_task(session._ws_writer())
        deadline = time.monotonic() + 0.5
        while len(session._ws.sent) < 2 and time.monotonic() < deadline:
            await asyncio.sleep(0)
        session._closed = True
        session._out_ready.set()
        await asyncio.wait_for(writer, timeout=1)
        types = [json.loads(item)["type"] for item in session._ws.sent]
        assert types[0] == "response.audio.delta"
        assert types.count("chat_message_delta") == 1
        transcript = next(json.loads(item) for item in session._ws.sent if json.loads(item)["type"] == "chat_message_delta")
        assert transcript["n"] == 7

    asyncio.run(run())


def _loud_stereo_frame(samples: int = 960, value: int = 1200, pts: int | None = None) -> AudioFrame:
    frame = AudioFrame(format="s16", layout="stereo", samples=samples)
    frame.sample_rate = 48000
    frame.planes[0].update(struct.pack("<h", value) * (samples * 2))
    if pts is not None:
        frame.pts = pts
        frame.time_base = Fraction(1, 48000)
    return frame


def test_audio_sender_forwards_contiguous_pcm_without_realtime_pacing():
    class FakeTrack:
        def __init__(self, frames: list[AudioFrame]):
            self._frames = list(frames)

        async def recv(self) -> AudioFrame:
            if not self._frames:
                raise RuntimeError("ended")
            return self._frames.pop(0)

    async def run():
        session = RealtimeSession(
            identity={},
            model="test",
            websocket=object(),
            access_token="token",
        )
        sent: list[tuple[str, dict]] = []

        async def capture(event_type: str, data: dict) -> None:
            sent.append((event_type, data))

        session._send_event = capture  # type: ignore[method-assign]
        session._remote_audio_track = FakeTrack([_loud_stereo_frame() for _ in range(10)])

        started = time.monotonic()
        await session._audio_sender()
        elapsed = time.monotonic() - started

        deltas = [
            base64.b64decode(data["delta"])
            for event_type, data in sent
            if event_type == "response.audio.delta"
        ]
        pcm = b"".join(deltas)
        assert elapsed < 0.15
        assert len(pcm) >= 10 * 960 * 2
        assert pcm[:2] != b"\x00\x00"
        assert sent[-1][0] == "response.audio.done"

    asyncio.run(run())


def _sine_pcm(seconds: float, freq: float = 440, amplitude: int = 8000) -> bytes:
    count = int(48_000 * seconds)
    return struct.pack(
        "<" + "h" * count,
        *[int(amplitude * math.sin(2 * math.pi * freq * index / 48_000)) for index in range(count)],
    )


def test_assembler_keeps_phrase_pauses_instead_of_ending_early():
    assembler = PcmOutputAssembler()
    events = []
    loud = _sine_pcm(0.04)
    quiet = b"\x00\x00" * 960
    events.extend(assembler.push(loud))
    for _ in range(18):
        events.extend(assembler.push(quiet))
    events.extend(assembler.push(loud))
    assert not any(event.kind == "done" for event in events)
    pcm = b"".join(event.pcm for event in events if event.kind == "delta")
    assert len(pcm) >= len(loud) * 2


def test_shallow_keep_playing_strategy_underruns_under_jitter():
    class KeepPlayingBuffer(PlaybackJitterBuffer):
        def pull(self, n: int):
            import array
            out = array.array("h", [0] * n)
            if not self.playing:
                if len(self._samples) >= self.prefill:
                    self.playing = True
                else:
                    return out
            got = min(n, len(self._samples))
            if got:
                out[:got] = self._samples[:got]
                del self._samples[:got]
                self.played += got
            if got < n:
                self.underrun_samples += n - got
            return out

    report = simulate_ws_playout(
        _sine_pcm(3.0),
        assembler=PcmOutputAssembler(chunk_bytes=int(48_000 * 0.08) * 2, max_silence_bytes=int(48_000 * 0.45) * 2),
        buffer=KeepPlayingBuffer(prefill_seconds=0.16, max_prefill_seconds=0.16),
        jitter_max_s=0.08,
        spike_every=8,
        spike_s=0.12,
    )
    assert report.underrun_samples > 0


def test_jitter_buffer_plays_continuous_speech_without_underrun():
    report = simulate_ws_playout(
        _sine_pcm(4.0),
        jitter_max_s=0.08,
        spike_every=12,
        spike_s=0.12,
    )
    assert report.done_events == 1
    assert report.underrun_samples == 0
    assert report.rebuffer_events == 0
    assert report.played >= int(48_000 * 3.5)


def test_jitter_buffer_stays_gapless_under_brutal_network_jitter():
    report = simulate_ws_playout(
        _sine_pcm(6.0),
        jitter_max_s=0.12,
        spike_every=6,
        spike_s=0.22,
    )
    assert report.underrun_samples == 0
    assert report.rebuffer_events == 0
    assert report.played >= int(48_000 * 5.85)


def test_jitter_buffer_survives_phrase_pause_without_cutting_audio():
    speech = _sine_pcm(1.0)
    pause = b"\x00\x00" * int(48_000 * 0.35)
    report = simulate_ws_playout(
        speech + pause + speech,
        jitter_max_s=0.06,
        spike_every=0,
        spike_s=0.0,
    )
    assert report.done_events == 1
    assert report.underrun_samples == 0
    assert report.rebuffer_events == 0


def test_assembler_keeps_paragraph_pause_in_long_speech():
    assembler = PcmOutputAssembler()
    events = []
    events.extend(assembler.push(_sine_pcm(0.04)))
    quiet = b"\x00\x00" * 960
    for _ in range(60):
        events.extend(assembler.push(quiet))
    events.extend(assembler.push(_sine_pcm(0.04)))
    assert not any(event.kind == "done" for event in events)


def test_long_speech_with_clock_drift_does_not_underrun():
    speech = _sine_pcm(8.0)
    pause = b"\x00\x00" * int(48_000 * 1.2)
    report = simulate_ws_playout(
        speech + pause + speech,
        jitter_max_s=0.08,
        spike_every=10,
        spike_s=0.14,
        consume_rate=1.0015,
    )
    assert report.done_events == 1
    assert report.underrun_samples == 0
    assert report.rebuffer_events == 0
    assert report.played >= int(48_000 * 16.5)


def test_assembler_does_not_end_on_long_mid_utterance_silence():
    assembler = PcmOutputAssembler()
    events = []
    events.extend(assembler.push(_sine_pcm(0.04)))
    quiet = b"\x00\x00" * 960
    for _ in range(int(3.2 / 0.02)):
        events.extend(assembler.push(quiet))
    events.extend(assembler.push(_sine_pcm(0.04)))
    assert not any(event.kind == "done" for event in events)
    assert assembler.speaking is True


def test_jitter_buffer_keeps_playing_across_long_dtx_hole():
    report = simulate_ws_playout(
        _sine_pcm(3.0),
        jitter_max_s=0.04,
        spike_every=15,
        spike_s=0.80,
    )
    assert report.rebuffer_events == 0
    assert report.played >= int(48_000 * 2.5)


def test_jitter_buffer_resumes_immediately_if_audio_arrives_after_done():
    buffer = PlaybackJitterBuffer()
    buffer.push(_sine_pcm(0.5))
    while not buffer.playing:
        buffer.pull(128)
    buffer.end()
    buffer.pull(128)
    buffer.push(_sine_pcm(0.3))
    assert buffer.ending is False
    assert buffer.playing is True
    played = buffer.pull(128)
    assert any(sample != 0 for sample in played)


def test_silence_to_cover_gap_uses_media_clock_not_wall_stall():
    assert silence_to_cover_gap(
        speaking=True,
        media_now=0.26,
        last_media=0.04,
        last_duration=0.02,
        waited=0.22,
    ) == pytest.approx(0.20, abs=0.001)
    assert silence_to_cover_gap(
        speaking=True,
        media_now=0.06,
        last_media=0.04,
        last_duration=0.02,
        waited=0.22,
    ) == 0.0
    assert silence_to_cover_gap(
        speaking=False,
        media_now=0.26,
        last_media=0.04,
        last_duration=0.02,
        waited=0.22,
    ) == 0.0
    assert silence_to_cover_gap(
        speaking=True,
        media_now=None,
        last_media=None,
        last_duration=0.0,
        waited=0.22,
    ) == pytest.approx(0.18, abs=0.001)


def test_jitter_buffer_plays_at_unity_rate_when_overfilled():
    buffer = PlaybackJitterBuffer()
    buffer.push(_sine_pcm(2.0))
    before = buffer.available
    for _ in range(200):
        buffer.pull(128)
    assert before - buffer.available == 200 * 128
    assert buffer.rate_adjusts == 0


def test_jitter_buffer_drops_oldest_when_full():
    buffer = PlaybackJitterBuffer(prefill_seconds=0.05, max_prefill_seconds=0.05, max_seconds=0.2)
    buffer.push(_sine_pcm(0.2, freq=200))
    buffer.push(_sine_pcm(0.2, freq=1800))
    assert buffer.available <= buffer.max_samples
    assert buffer.dropped_incoming > 0


def test_audio_sender_fills_dtx_gaps_with_silence():
    class DelayedTrack:
        def __init__(self):
            self.n = 0
            self.pts = 0

        async def recv(self) -> AudioFrame:
            self.n += 1
            if self.n > 8:
                raise RuntimeError("ended")
            if self.n == 4:
                await asyncio.sleep(0.22)
                self.pts += int(0.22 * 48_000)
            frame = _loud_stereo_frame(pts=self.pts)
            self.pts += 960
            return frame

    async def run():
        session = RealtimeSession(
            identity={},
            model="test",
            websocket=object(),
            access_token="token",
        )
        sent: list[tuple[str, dict]] = []

        async def capture(event_type: str, data: dict) -> None:
            sent.append((event_type, data))

        session._send_event = capture  # type: ignore[method-assign]
        session._remote_audio_track = DelayedTrack()
        await session._audio_sender()
        deltas = [
            base64.b64decode(data["delta"])
            for event_type, data in sent
            if event_type == "response.audio.delta"
        ]
        pcm = b"".join(deltas)
        assert any(pcm[index:index + 2] == b"\x00\x00" for index in range(0, len(pcm), 2))
        assert sent[-1][0] == "response.audio.done"

    asyncio.run(run())


def test_audio_sender_does_not_pad_event_loop_stall_without_pts_jump():
    class StallingTrack:
        def __init__(self):
            self.n = 0
            self.pts = 0

        async def recv(self) -> AudioFrame:
            self.n += 1
            if self.n > 6:
                raise RuntimeError("ended")
            if self.n == 4:
                await asyncio.sleep(0.22)
            frame = _loud_stereo_frame(pts=self.pts)
            self.pts += 960
            return frame

    async def run():
        session = RealtimeSession(
            identity={},
            model="test",
            websocket=object(),
            access_token="token",
        )
        sent: list[tuple[str, dict]] = []

        async def capture(event_type: str, data: dict) -> None:
            sent.append((event_type, data))

        session._send_event = capture  # type: ignore[method-assign]
        session._remote_audio_track = StallingTrack()
        await session._audio_sender()
        pcm = b"".join(
            base64.b64decode(data["delta"])
            for event_type, data in sent
            if event_type == "response.audio.delta"
        )
        assert not any(pcm[index:index + 2] == b"\x00\x00" for index in range(0, len(pcm), 2))

    asyncio.run(run())


def test_dc_reader_requests_audio_flush_when_returning_to_listening():
    async def run():
        session = RealtimeSession(identity={}, model="test", websocket=object(), access_token="token")
        session._data_channel = object()
        sent: list[str] = []

        async def capture(event_type: str, data: dict) -> None:
            sent.append(event_type)

        session._send_event = capture  # type: ignore[method-assign]
        session._queue_dc_message(json.dumps({
            "type": "data_message",
            "data": json.dumps({"type": "state_update", "payload": {"new_state": "listening"}}),
        }))
        reader = asyncio.create_task(session._dc_reader())
        deadline = time.monotonic() + 0.5
        while not session._end_audio_output and time.monotonic() < deadline:
            await asyncio.sleep(0)
        assert session._end_audio_output is True
        assert "state_update" in sent
        assert "listening" in END_AUDIO_STATES
        session._closed = True
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)

    asyncio.run(run())
