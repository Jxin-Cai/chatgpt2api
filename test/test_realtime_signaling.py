from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import realtime
from services.realtime.signaling import RealtimeSignalingGuard


def test_direct_realtime_signaling_proxies_sdp_without_exposing_upstream_token():
    app = FastAPI()
    app.include_router(realtime.create_router())

    with (
        mock.patch.object(realtime, "require_identity", return_value={"name": "tester"}),
        mock.patch.object(
            realtime.account_service,
            "get_realtime_access_token",
            return_value="upstream-secret-token",
        ),
        mock.patch.object(
            realtime,
            "exchange_realtime_sdp",
            new=mock.AsyncMock(return_value=("answer-sdp", "session-location")),
        ) as exchange,
    ):
        response = TestClient(app).post(
            "/v1/realtime/sessions",
            headers={"Authorization": "Bearer client-key"},
            json={"sdp": "v=0\r\n" + "a=x\r\n" * 30, "voice": "cove"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["sdp"] == "answer-sdp"
    assert payload["location"] == "session-location"
    assert payload["attempt_id"]
    assert payload["request_id"] == response.headers["X-Request-ID"]
    exchange.assert_awaited_once()
    assert exchange.await_args.kwargs["access_token"] == "upstream-secret-token"
    assert "upstream-secret-token" not in response.text


def test_realtime_capabilities_and_voices_require_api_key():
    app = FastAPI()
    app.include_router(realtime.create_router())
    client = TestClient(app)

    unauthorized = client.get("/v1/realtime/capabilities")
    assert unauthorized.status_code == 401

    with mock.patch.object(realtime, "require_identity", return_value={"name": "tester"}):
        capabilities = client.get(
            "/v1/realtime/capabilities",
            headers={"Authorization": "Bearer client-key"},
        )
        voices = client.get(
            "/v1/realtime/voices",
            headers={"Authorization": "Bearer client-key"},
        )

    assert capabilities.status_code == 200
    assert capabilities.json()["transports"]["webrtc"]["recommended"] is True
    assert capabilities.json()["transports"]["websocket"]["input_audio_format"] == "pcm16_mono_48000"
    assert capabilities.json()["authentication"] == "bearer"
    assert voices.status_code == 200
    assert voices.json()["data"][0]["id"] == capabilities.json()["default_voice"]
    assert {voice["id"] for voice in voices.json()["data"]} == realtime.REALTIME_VOICE_IDS


def test_realtime_signaling_rejects_unknown_voice_before_contacting_upstream():
    app = FastAPI()
    app.include_router(realtime.create_router())

    response = TestClient(app).post(
        "/v1/realtime/sessions",
        headers={"Authorization": "Bearer client-key"},
        json={"sdp": "v=0\r\n" + "a=x\r\n" * 30, "voice": "unknown"},
    )

    assert response.status_code == 422


def test_realtime_signaling_retry_excludes_previously_selected_account():
    app = FastAPI()
    app.include_router(realtime.create_router())
    selected: list[set[str]] = []

    def select(excluded=None):
        selected.append(set(excluded or set()))
        return "first-token" if "first-token" not in selected[-1] else "second-token"

    with (
        mock.patch.object(realtime, "require_identity", return_value={"id": "retry-user", "name": "tester"}),
        mock.patch.object(realtime.account_service, "get_realtime_access_token", side_effect=select),
        mock.patch.object(
            realtime,
            "exchange_realtime_sdp",
            new=mock.AsyncMock(return_value=("answer-sdp", "session-location")),
        ),
    ):
        client = TestClient(app)
        first = client.post(
            "/v1/realtime/sessions",
            headers={"Authorization": "Bearer client-key"},
            json={"sdp": "v=0\r\n" + "a=x\r\n" * 30},
        )
        second = client.post(
            "/v1/realtime/sessions",
            headers={"Authorization": "Bearer client-key"},
            json={
                "sdp": "v=0\r\n" + "a=x\r\n" * 30,
                "attempt_id": first.json()["attempt_id"],
            },
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert selected == [set(), {"first-token"}]


def test_realtime_signaling_hides_upstream_error_body():
    app = FastAPI()
    app.include_router(realtime.create_router())

    with (
        mock.patch.object(realtime, "require_identity", return_value={"id": "error-user", "name": "tester"}),
        mock.patch.object(realtime.account_service, "get_realtime_access_token", return_value="token"),
        mock.patch.object(realtime.account_service, "mark_realtime_unavailable") as mark_unavailable,
        mock.patch.object(
            realtime,
            "exchange_realtime_sdp",
            new=mock.AsyncMock(
                side_effect=realtime.UpstreamSignalingError(403, "private upstream response and token")
            ),
        ),
    ):
        response = TestClient(app).post(
            "/v1/realtime/sessions",
            headers={"Authorization": "Bearer client-key"},
            json={"sdp": "v=0\r\n" + "a=x\r\n" * 30},
        )

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "realtime_upstream_error"
    assert response.json()["error"]["retryable"] is True
    assert response.json()["attempt_id"]
    assert "private upstream" not in response.text
    mark_unavailable.assert_called_once()


def test_realtime_signaling_classifies_upstream_quota_and_persists_limit():
    app = FastAPI()
    app.include_router(realtime.create_router())

    with (
        mock.patch.object(realtime, "require_identity", return_value={"id": "quota-user", "name": "tester"}),
        mock.patch.object(realtime.account_service, "get_realtime_access_token", return_value="quota-token"),
        mock.patch.object(realtime.account_service, "mark_realtime_unavailable") as mark_unavailable,
        mock.patch.object(
            realtime,
            "exchange_realtime_sdp",
            new=mock.AsyncMock(side_effect=realtime.UpstreamSignalingError(403, "Daily limit reached")),
        ),
    ):
        response = TestClient(app).post(
            "/v1/realtime/sessions",
            headers={"Authorization": "Bearer client-key"},
            json={"sdp": "v=0\r\n" + "a=x\r\n" * 30},
        )

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "realtime_voice_quota_limited"
    assert response.json()["attempt_id"]
    mark_unavailable.assert_called_once()
    assert mark_unavailable.call_args.kwargs["status"] == "limited"


def test_failed_signaling_returns_attempt_id_and_retry_excludes_failed_account():
    app = FastAPI()
    guard = RealtimeSignalingGuard(max_concurrency=1, rate_per_minute=10, attempt_ttl_seconds=30)
    selected: list[set[str]] = []

    def select(excluded=None):
        excluded = set(excluded or set())
        selected.append(excluded)
        return "healthy-token" if "failed-token" in excluded else "failed-token"

    exchange = mock.AsyncMock(
        side_effect=[
            realtime.UpstreamSignalingError(403, "forbidden"),
            ("v=0\r\na=answer\r\n", "/v1/wm/rtc-retry"),
        ]
    )
    with (
        mock.patch.object(realtime, "require_identity", return_value={"id": "retry-error-user", "name": "tester"}),
        mock.patch.object(realtime.account_service, "get_realtime_access_token", side_effect=select),
        mock.patch.object(realtime.account_service, "mark_realtime_unavailable"),
        mock.patch.object(realtime.account_service, "mark_realtime_available"),
        mock.patch.object(realtime, "realtime_signaling_guard", guard),
        mock.patch.object(realtime, "exchange_realtime_sdp", new=exchange),
    ):
        app.include_router(realtime.create_router())
        first = TestClient(app).post(
            "/v1/realtime/sessions",
            headers={"Authorization": "Bearer client-key"},
            json={"sdp": "v=0\r\n" + "a=x\r\n" * 30},
        )
        second = TestClient(app).post(
            "/v1/realtime/sessions",
            headers={"Authorization": "Bearer client-key"},
            json={
                "sdp": "v=0\r\n" + "a=x\r\n" * 30,
                "attempt_id": first.json()["attempt_id"],
            },
        )

    assert first.status_code == 502
    assert second.status_code == 200
    assert selected == [set(), {"failed-token"}]


def test_realtime_quota_report_requires_matching_attempt_identity():
    app = FastAPI()
    app.include_router(realtime.create_router())

    with mock.patch.object(realtime, "require_identity", return_value={"id": "quota-user", "name": "tester"}):
        with mock.patch.object(
            realtime.realtime_signaling_guard,
            "mark_quota_exhausted",
            return_value="account-token",
        ) as mark:
            with mock.patch.object(realtime.account_service, "mark_realtime_unavailable") as persist:
                response = TestClient(app).post(
                    "/v1/realtime/sessions/attempt-123456789/quota-exhausted",
                    headers={"Authorization": "Bearer client-key"},
                    json={"reason": "cap_reached", "retry_after_seconds": 7200},
                )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    mark.assert_called_once_with("quota-user", "attempt-123456789", 7200)
    persist.assert_called_once()
    assert persist.call_args.kwargs["cooldown_seconds"] == 7200
