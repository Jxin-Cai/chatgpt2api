import asyncio
import json
import time

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
from services.realtime.session import (
    RealtimeQuotaExceeded,
    RealtimeSession,
    resample_to_pcm16_mono,
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
