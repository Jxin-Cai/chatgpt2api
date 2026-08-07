from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import realtime


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
    assert response.json() == {"sdp": "answer-sdp", "location": "session-location"}
    exchange.assert_awaited_once()
    assert exchange.await_args.kwargs["access_token"] == "upstream-secret-token"
    assert "upstream-secret-token" not in response.text
