import unittest
from unittest import mock

from services import sub2api_service


class _FakeResponse:
    ok = True
    status_code = 200
    text = ""

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, payload):
        self.payload = payload
        self.requests = []

    def get(self, url, headers=None, params=None, timeout=None):
        self.requests.append({"url": url, "headers": headers, "params": params, "timeout": timeout})
        return _FakeResponse(self.payload)

    def close(self):
        pass


class Sub2APIServiceTest(unittest.TestCase):
    def test_list_remote_accounts_keeps_items_without_list_credentials_token(self) -> None:
        session = _FakeSession({
            "code": 0,
            "message": "ok",
            "data": {
                "items": [
                    {
                        "id": 123,
                        "name": "user@example.com",
                        "status": "active",
                        "credentials": {},
                    }
                ],
                "total": 1,
            },
        })

        with mock.patch.object(sub2api_service, "_auth_headers", return_value={"x-api-key": "test"}):
            with mock.patch.object(sub2api_service, "Session", return_value=session):
                accounts = sub2api_service.list_remote_accounts({"base_url": "http://sub2api.test"})

        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0]["id"], "123")
        self.assertEqual(accounts[0]["email"], "user@example.com")
        self.assertEqual(session.requests[0]["params"]["platform"], "openai")
        self.assertEqual(session.requests[0]["params"]["type"], "oauth")


if __name__ == "__main__":
    unittest.main()
