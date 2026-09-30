"""Contract tests use synthetic upstream audio; no account or external API calls."""
import asyncio
import base64
import json
import math
import struct
from unittest.mock import AsyncMock

import pytest
from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import live
from services.live.protocol import LiveError, PcmConverter, TranscriptAdapter, parse_config
from services.live.session import LiveSession
from services.realtime.audio_track import BufferedAudioStreamTrack
from services.realtime.session import RealtimeSession
from services.realtime.signaling import RealtimeSignalingGuard


class FakeChannel:
    readyState = "open"
    def __init__(self):
        self.sent = []
    def send(self, value):
        self.sent.append(json.loads(value))


async def fake_start(self):
    self._pc = type("Peer", (), {"close": AsyncMock()})()
    self._input_track = BufferedAudioStreamTrack()
    self._remote_audio_track = self._input_track  # synthetic upstream echoes media
    self._data_channel = FakeChannel()
    self._location = "private-upstream-location"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(live, "live_runtime", live.LiveRuntime())
    monkeypatch.setattr(live, "realtime_signaling_guard", RealtimeSignalingGuard(rate_per_minute=100))
    monkeypatch.setattr(live, "require_identity", lambda auth: {"id": "test-user"})
    monkeypatch.setattr(live.account_service, "get_realtime_access_token", lambda *args: "dummy")
    monkeypatch.setattr(LiveSession, "_start", fake_start)
    app = FastAPI()
    app.include_router(live.create_router())
    with TestClient(app) as c:
        yield c


def pcm(rate, seconds=.04):
    return b"".join(struct.pack("<h", int(6000 * math.sin(2 * math.pi * 440 * i / rate))) for i in range(int(rate * seconds)))


@pytest.mark.parametrize("rate", [16000, 24000])
def test_live_pcm_conversion_retains_pitch_and_duration(rate):
    up = PcmConverter(rate, 48000)
    down = PcmConverter(48000, rate)
    source = pcm(rate, 1)
    output = b"".join(down.push(up.push(source[i:i + rate // 25])) for i in range(0, len(source), rate // 25))
    assert abs(len(output) - len(source)) < rate * .01 * 2
    samples = struct.unpack(f"<{len(output)//2}h", output)
    crossings = sum(a <= 0 < b for a, b in zip(samples, samples[1:]))
    assert 435 <= crossings <= 441


@pytest.mark.parametrize("patch,param", [
    ({"model": "gpt-realtime"}, "session.model"),
    ({"audio": {"format": {"type": "audio/pcm", "rate": 48000}}}, "session.audio.format"),
    ({"audio": {"output": {"voice": {"id": "voice_custom"}}}}, "session.audio.output.voice"),
    ({"audio": {"output": {"voice": "ember"}}}, "session.audio.output.voice"),
    ({"delegation": {"type": "responses", "responses": {"model": "any"}}}, "session.delegation"),
    ({"store": True}, "session.store"),
    ({"turn_detection": {"type": "semantic_vad"}}, "session.turn_detection"),
    ({"client": {}}, "session.client"),
])
def test_config_rejects_unimplemented_semantics(patch, param):
    with pytest.raises(LiveError) as error:
        parse_config({"model": "gpt-live-1", **patch})
    assert error.value.param == param


def test_webrtc_format_is_negotiated_and_history_is_bounded():
    config = parse_config({"model": "gpt-live-1"}, "webrtc")
    assert "format" not in config.public["audio"]
    with pytest.raises(LiveError):
        parse_config({"model": "gpt-live-1", "audio": {"format": {"type": "audio/pcm", "rate": 24000}}}, "webrtc")
    with pytest.raises(LiveError):
        parse_config({"model": "gpt-live-1", "input": [{"role": "user", "content": [{"text": "x"}]}] * 129})


def test_transcripts_preserve_every_increment_and_interleaved_speakers():
    adapter = TranscriptAdapter()
    def add(channel, role):
        return {"delta": {"c": channel, "o": "add", "v": {"message": {"id": str(channel), "author": {"role": role}, "content": {"parts": []}}}}}
    def patch(channel, text):
        return {"delta": {"c": channel, "v": [{"o": "append", "p": "/message/content/parts/0/text", "v": text}]}}
    adapter.push(add(1, "user"))
    adapter.push(add(2, "assistant"))
    assert adapter.push(patch(1, "你")) == ("user", "你")
    assert adapter.push(patch(2, "嗯")) == ("assistant", "嗯")
    assert adapter.push(patch(1, " 好")) == ("user", " 好")
    assert adapter.push(patch(1, " 好")) == ("user", " 好")


def test_transcript_part_patches_do_not_duplicate_or_erase_previous_parts():
    adapter = TranscriptAdapter()
    assert adapter.push({"delta": {"o": "add", "v": {"message": {
        "id": "a", "author": {"role": "assistant"}, "content": {"parts": ["Hello"]},
    }}}}) == ("assistant", "Hello")
    def patch(operation, path, value):
        return adapter.push({"delta": {"v": [{"o": operation, "p": path, "v": value}]}})
    assert patch("append", "/message/content/parts/0", " there") == ("assistant", " there")
    assert patch("add", "/message/content/parts/1", {"text": "!"}) == ("assistant", "!")
    assert patch("replace", "/message/content/parts/1/text", "!!") == ("assistant", "!")
    assert patch("replace", "/message/content/parts/1/text", "!!") is None
    with pytest.raises(LiveError, match="revised"):
        patch("replace", "/message/content/parts/0", "Different")


@pytest.mark.parametrize("field,value", [("role", []), ("status", {}), ("content", [{"type": [], "text": "Hi"}])])
def test_malformed_history_values_are_validation_errors(field, value):
    item = {"role": "user", "content": [{"type": "input_text", "text": "Hi"}], field: value}
    with pytest.raises(LiveError):
        parse_config({"model": "gpt-live-1", "input": [item]})


def test_failed_webrtc_setup_releases_reservation_and_peer(client, monkeypatch):
    transport = type("Transport", (), {"answer": AsyncMock(side_effect=ValueError("bad SDP")), "close": AsyncMock()})()
    monkeypatch.setattr(live, "WebRTCTransport", lambda: transport)
    response = client.post("/v1/live/sessions", json={"session": {"model": "gpt-live-1"}, "transport": {"type": "webrtc", "sdp": "x" * 120}})
    assert response.status_code == 400
    transport.close.assert_awaited_once()
    assert not live.live_runtime.sessions


def test_websocket_starts_only_after_session_start_and_closes_with_usage(client):
    with client.websocket_connect("/v1/live/sessions", headers={"Authorization": "Bearer dummy"}) as ws:
        ws.send_json({"type": "session.start", "event_id": "start-1", "session": {"model": "gpt-live-1"}})
        started = ws.receive_json()
        assert started["type"] == "session.started"
        assert started["client_event_id"] == "start-1"
        assert started["session"]["model"] == "gpt-live-1"
        assert started["session"]["audio"]["format"]["rate"] == 24000
        assert started["session"]["id"].startswith("live_")
        assert "private" not in json.dumps(started)
        ws.send_json({"type": "session.input_audio.mute", "event_id": "mute-1"})
        assert ws.receive_json()["type"] == "session.input_audio.muted"
        ws.send_json({"type": "session.input_audio.unmute"})
        assert ws.receive_json()["type"] == "session.input_audio.unmuted"
        ws.send_json({"type": "session.close", "event_id": "close-1"})
        closed = ws.receive_json()
        assert closed["type"] == "session.closed"
        assert closed["reason"] == "close_requested"
        assert closed["session"]["id"] == started["session"]["id"]
        assert closed["client_event_id"] == "close-1"
        assert closed["usage"]["seconds"] >= 0
    assert not live.live_runtime.sessions


def test_websocket_rejects_wrong_first_command_and_query_model(client):
    with client.websocket_connect("/v1/live/sessions") as ws:
        ws.send_json({"type": "input_audio_buffer.append", "audio": ""})
        assert ws.receive_json()["error"]["code"] == "unknown_parameter"
    with client.websocket_connect("/v1/live/sessions?model=gpt-live-1") as ws:
        assert ws.receive_json()["error"]["code"] == "unknown_parameter"


@pytest.mark.parametrize("rate", [16000, 24000])
def test_websocket_real_pcm_echo_has_negotiated_rate(client, rate):
    with client.websocket_connect("/v1/live/sessions") as ws:
        ws.send_json({"type": "session.start", "session": {"model": "gpt-live-1", "audio": {"format": {"type": "audio/pcm", "rate": rate}}}})
        assert ws.receive_json()["type"] == "session.started"
        ws.send_json({"type": "session.input_audio.append", "audio": base64.b64encode(pcm(rate, .08)).decode()})
        message = ws.receive_json()
        assert message["type"] == "session.output_audio.delta"
        data = base64.b64decode(message["delta"])
        assert rate * .03 * 2 <= len(data) <= rate * .041 * 2
        assert any(data)
        ws.send_json({"type": "session.close"})
        while ws.receive_json()["type"] != "session.closed":
            pass


def test_invalid_commands_have_correlation_and_do_not_kill_live_session(client):
    with client.websocket_connect("/v1/live/sessions") as ws:
        ws.send_json({"type": "session.start", "session": {"model": "gpt-live-1"}})
        ws.receive_json()
        for data, code in [
            ({"type": []}, "invalid_value"),
            ({"type": "session.update", "session": {"audio": {"output": {"voice": "cedar"}}}}, "immutable_field_update"),
            ({"type": "response.create"}, "unsupported_event"),
            ({"type": "session.thinking.append", "content": "silent", "delegation_id": None}, "unsupported_event"),
            ({"type": "session.input_audio.append", "audio": "?"}, "invalid_value"),
            ({"type": "session.input_audio.append", "audio": "AA=="}, "invalid_value"),
        ]:
            ws.send_json({**data, "event_id": "bad-1"})
            error = ws.receive_json()
            assert error["type"] == "error"
            assert error["error"]["code"] == code
            assert error["error"]["client_event_id"] == "bad-1"
        ws.send_json([1, 2])
        assert ws.receive_json()["error"]["code"] == "invalid_value"
        ws.send_json({"type": "session.instructions.append", "content": "Speak briefly", "delegation_id": None, "event_id": "context-1"})
        ack = ws.receive_json()
        assert ack["type"] == "session.instructions.appended"
        assert ack["client_event_id"] == "context-1"
        ws.send_json({"type": "session.close"})
        assert ws.receive_json()["type"] == "session.closed"


def test_live_audio_and_transcripts_have_only_standard_events():
    class Sink:
        def __init__(self): self.events = []
        async def send_text(self, value): self.events.append(json.loads(value))
    async def run():
        sink = Sink()
        session = LiveSession(identity={}, websocket=sink, access_token="dummy", config=parse_config({"model": "gpt-live-1"}))
        await session._send_event("startup_telemetry", {"conversation_id": "private"})
        await session._send_event("response.audio.done", {})
        await session._send_event("response.audio.delta", {"delta": base64.b64encode(pcm(48000)).decode()})
        await session._send_event("chat_message_delta", {"delta": {"o": "add", "v": {"message": {"id": "x", "author": {"role": "assistant"}, "content": {"parts": []}}}}})
        for text in ["你", "好", " 好"]:
            await session._send_event("chat_message_delta", {"delta": {"v": [{"p": "/message/content/parts/0/text", "o": "append", "v": text}]}})
        assert [e["type"] for e in sink.events] == ["session.output_audio.delta"] + ["session.output_transcript.delta"] * 3
        assert "".join(e["delta"] for e in sink.events[1:]) == "你好 好"
        assert len(base64.b64decode(sink.events[0]["delta"])) < len(pcm(48000))
        assert all("event_id" in e for e in sink.events)
    asyncio.run(run())


def test_interrupt_discards_queued_audio_and_blocks_late_frames():
    async def run():
        session = LiveSession(identity={}, websocket=object(), access_token="dummy", config=parse_config({"model": "gpt-live-1"}))
        session._writer_started = True
        frame = {"delta": base64.b64encode(pcm(48000)).decode()}
        await session._send_event("response.audio.delta", frame)
        assert session._audio_out.qsize() == 1
        await session._send_event("input_audio_buffer.speech_started", {})
        await session._send_event("response.audio.delta", frame)
        assert session._audio_out.empty()
        await session._send_event("state_update", {"new_state": "speaking"})
        await session._send_event("response.audio.delta", frame)
        assert session._audio_out.qsize() == 1
    asyncio.run(run())


def test_call_quota_binding_outlives_short_attempt(monkeypatch):
    monkeypatch.setattr("services.realtime.signaling.time.monotonic", lambda: 1000)
    guard = RealtimeSignalingGuard(attempt_ttl_seconds=300)
    call, _ = guard.open_attempt("user", None)
    guard.record_account(call, "dummy")
    monkeypatch.setattr("services.realtime.signaling.time.monotonic", lambda: 1301)
    guard.open_attempt("user", None)  # prune attempts
    assert guard.mark_quota_exhausted("user", call) == "dummy"
    assert guard.mark_quota_exhausted("another", call) is None


def test_http_rejects_bad_config_before_opening_upstream(client):
    response = client.post("/v1/live/sessions", json={"session": {"model": "gpt-live-1", "store": True}, "transport": {"type": "webrtc", "sdp": "x" * 120}})
    assert response.status_code == 400
    assert response.json()["error"]["param"] == "session.store"
    assert not live.live_runtime.sessions


def test_webrtc_real_loopback_uses_oai_events_and_public_live_lifecycle(client, monkeypatch):
    import services.live.transport as rtc_transport
    monkeypatch.setattr(rtc_transport, "RTCPeerConnection", lambda: RTCPeerConnection(RTCConfiguration(iceServers=[])))
    async def run():
        pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        mic = BufferedAudioStreamTrack()
        pc.addTrack(mic)
        tracks = asyncio.Queue()
        pc.on("track", lambda track: tracks.put_nowait(track))
        dc = pc.createDataChannel("oai-events")
        received = asyncio.Queue()
        dc.on("message", lambda message: received.put_nowait(json.loads(message)))
        try:
            await pc.setLocalDescription(await pc.createOffer())
            response = await asyncio.to_thread(client.post, "/v1/live/sessions", json={
                "session": {"model": "gpt-live-1", "audio": {"output": {"voice": "marin"}}},
                "transport": {"type": "webrtc", "sdp": pc.localDescription.sdp},
            })
            assert response.status_code == 201, response.text
            result = response.json()
            assert set(result) == {"session", "transport"}
            await pc.setRemoteDescription(RTCSessionDescription(sdp=result["transport"]["sdp"], type="answer"))
            started = await asyncio.wait_for(received.get(), timeout=10)
            assert started["type"] == "session.started"
            assert started["session"]["id"] == result["session"]["id"]
            assert "format" not in started["session"]["audio"]
            mic.push_pcm16(pcm(48000, .2))
            remote = await asyncio.wait_for(tracks.get(), timeout=5)
            for _ in range(100):
                frame = await asyncio.wait_for(remote.recv(), timeout=3)
                if abs(frame.to_ndarray()).max() > 500:
                    break
            else:
                pytest.fail("No audible PCM survived the browser/provider media bridge")
            dc.send(json.dumps({"type": "session.input_audio.mute", "event_id": "mute"}))
            assert (await asyncio.wait_for(received.get(), timeout=5))["type"] == "session.input_audio.muted"
            dc.send(json.dumps({"type": "session.close"}))
            closed = await asyncio.wait_for(received.get(), timeout=5)
            assert closed["type"] == "session.closed"
        finally:
            await pc.close()
    asyncio.run(run())
