import tempfile
import unittest
from pathlib import Path
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


class _FakeSequenceSession:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.requests = []

    def get(self, url, headers=None, params=None, timeout=None):
        self.requests.append({"url": url, "headers": headers, "params": params, "timeout": timeout})
        return _FakeResponse(self.payloads.pop(0))

    def close(self):
        pass


class Sub2APIServiceTest(unittest.TestCase):
    def _list_accounts(self, payload, **kwargs):
        session = _FakeSession(payload)
        with mock.patch.object(sub2api_service, "_auth_headers", return_value={"x-api-key": "test"}):
            with mock.patch.object(sub2api_service, "Session", return_value=session):
                accounts = sub2api_service.list_remote_accounts({"base_url": "http://sub2api.test"}, **kwargs)
        return accounts, session

    def test_list_remote_accounts_keeps_items_without_list_credentials_token(self) -> None:
        accounts, session = self._list_accounts({
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

        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0]["id"], "123")
        self.assertEqual(accounts[0]["email"], "user@example.com")
        self.assertFalse(accounts[0]["has_access_token"])
        self.assertNotIn("_access_token", accounts[0])
        self.assertEqual(session.requests[0]["params"]["platform"], "openai")
        self.assertEqual(session.requests[0]["params"]["type"], "oauth")

    def test_list_remote_accounts_marks_access_token_without_exposing_it(self) -> None:
        accounts, _session = self._list_accounts({
            "code": 0,
            "message": "ok",
            "data": {
                "items": [
                    {
                        "id": "account-1",
                        "name": "remote account",
                        "credentials": {
                            "email": "user@example.com",
                            "access_token": "secret-token",
                            "refresh_token": "refresh-token",
                        },
                    }
                ],
                "total": 1,
            },
        })

        self.assertEqual(len(accounts), 1)
        self.assertTrue(accounts[0]["has_access_token"])
        self.assertTrue(accounts[0]["has_refresh_token"])
        self.assertNotIn("access_token", accounts[0])
        self.assertNotIn("_access_token", accounts[0])

    def test_list_remote_accounts_can_return_internal_access_token(self) -> None:
        accounts, _session = self._list_accounts({
            "code": 0,
            "message": "ok",
            "data": {
                "items": [
                    {
                        "id": "account-1",
                        "credentials": {"accessToken": "camel-token"},
                    },
                    {
                        "id": "account-2",
                        "credentials": {"token": "short-token"},
                    },
                ],
                "total": 2,
            },
        }, include_access_token=True)

        self.assertEqual(accounts[0]["_access_token"], "camel-token")
        self.assertEqual(accounts[1]["_access_token"], "short-token")

    def test_list_remote_accounts_reads_top_level_and_json_credentials_tokens(self) -> None:
        accounts, _session = self._list_accounts({
            "code": 0,
            "message": "ok",
            "data": {
                "items": [
                    {
                        "id": "account-1",
                        "email": "top@example.com",
                        "access_token": "top-token",
                    },
                    {
                        "id": "account-2",
                        "credentials": '{"email":"json@example.com","access_token":"json-token"}',
                    },
                ],
                "total": 2,
            },
        }, include_access_token=True)

        self.assertEqual(accounts[0]["email"], "top@example.com")
        self.assertEqual(accounts[0]["_access_token"], "top-token")
        self.assertEqual(accounts[1]["email"], "json@example.com")
        self.assertEqual(accounts[1]["_access_token"], "json-token")

    def test_fetch_access_token_for_account_reads_nested_account_and_top_level_token(self) -> None:
        session = _FakeSession({
            "code": 0,
            "message": "ok",
            "data": {
                "account": {
                    "id": "account-1",
                    "email": "user@example.com",
                    "plan_type": "Plus",
                    "accessToken": "detail-token",
                }
            },
        })
        with mock.patch.object(sub2api_service, "_auth_headers", return_value={"x-api-key": "test"}):
            with mock.patch.object(sub2api_service, "Session", return_value=session):
                token, meta = sub2api_service._fetch_access_token_for_account(
                    {"base_url": "http://sub2api.test"},
                    "account-1",
                )

        self.assertEqual(token, "detail-token")
        self.assertEqual(meta["email"], "user@example.com")
        self.assertEqual(meta["plan_type"], "Plus")

    def test_fetch_access_token_for_account_falls_back_to_export_data(self) -> None:
        session = _FakeSequenceSession([
            {
                "code": 0,
                "message": "ok",
                "data": {
                    "id": "account-1",
                    "email": "redacted@example.com",
                    "credentials": {"email": "redacted@example.com"},
                },
            },
            {
                "code": 0,
                "message": "ok",
                "data": {
                    "accounts": [
                        {
                            "name": "user@example.com",
                            "platform": "openai",
                            "type": "oauth",
                            "credentials": {
                                "email": "user@example.com",
                                "plan_type": "Pro",
                                "access_token": "export-token",
                            },
                        }
                    ],
                    "proxies": [],
                },
            },
        ])
        with mock.patch.object(sub2api_service, "_auth_headers", return_value={"x-api-key": "test"}):
            with mock.patch.object(sub2api_service, "Session", return_value=session):
                token, meta = sub2api_service._fetch_access_token_for_account(
                    {"base_url": "http://sub2api.test"},
                    "account-1",
                )

        self.assertEqual(token, "export-token")
        self.assertEqual(meta["email"], "user@example.com")
        self.assertEqual(meta["plan_type"], "Pro")
        self.assertEqual(session.requests[1]["url"], "http://sub2api.test/api/v1/admin/accounts/data")
        self.assertEqual(session.requests[1]["params"], {"ids": "account-1", "include_proxies": "false"})

    def test_run_import_from_list_tokens_falls_back_to_detail_fetch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = sub2api_service.Sub2APIConfig(Path(temp_dir) / "sub2api_config.json")
            server = config.add_server(name="", base_url="http://sub2api.test", email="", password="", api_key="key")
            service = sub2api_service.Sub2APIImportService(config)
            config.set_import_job(server["id"], {
                "job_id": "job",
                "status": "pending",
                "created_at": "now",
                "updated_at": "now",
                "total": 2,
                "completed": 0,
                "added": 0,
                "skipped": 0,
                "refreshed": 0,
                "failed": 0,
                "errors": [],
            })

            with mock.patch.object(sub2api_service, "list_remote_accounts", return_value=[
                {"id": "a", "_access_token": "token-a"},
                {"id": "b"},
            ]):
                with mock.patch.object(sub2api_service, "_fetch_access_token_for_account", return_value=("token-b", {})) as detail_mock:
                    with mock.patch.object(sub2api_service.account_service, "add_accounts", return_value={"added": 2, "skipped": 0, "items": []}) as add_mock:
                        with mock.patch.object(sub2api_service.account_service, "refresh_accounts", return_value={"refreshed": 2, "errors": [], "items": []}):
                            service._run_import_from_list_tokens(server["id"], server, ["a", "b"])

            detail_mock.assert_called_once_with(server, "b")
            add_mock.assert_called_once_with(["token-a", "token-b"], source_type="codex")
            job = config.get_import_job(server["id"])
            self.assertEqual(job["status"], "completed")
            self.assertEqual(job["failed"], 0)

    def test_run_import_from_list_tokens_imports_without_detail_fetch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = sub2api_service.Sub2APIConfig(Path(temp_dir) / "sub2api_config.json")
            server = config.add_server(name="", base_url="http://sub2api.test", email="", password="", api_key="key")
            service = sub2api_service.Sub2APIImportService(config)
            config.set_import_job(server["id"], {
                "job_id": "job",
                "status": "pending",
                "created_at": "now",
                "updated_at": "now",
                "total": 2,
                "completed": 0,
                "added": 0,
                "skipped": 0,
                "refreshed": 0,
                "failed": 0,
                "errors": [],
            })

            with mock.patch.object(sub2api_service, "list_remote_accounts", return_value=[
                {"id": "a", "_access_token": "token-a"},
                {"id": "b", "_access_token": "token-b"},
            ]) as list_mock:
                with mock.patch.object(sub2api_service, "_fetch_access_token_for_account") as detail_mock:
                    with mock.patch.object(sub2api_service.account_service, "add_accounts", return_value={"added": 2, "skipped": 0, "items": []}) as add_mock:
                        with mock.patch.object(sub2api_service.account_service, "refresh_accounts", return_value={"refreshed": 2, "errors": [], "items": []}) as refresh_mock:
                            service._run_import_from_list_tokens(server["id"], server, ["a", "b"])

            list_mock.assert_called_once_with(server, include_access_token=True)
            detail_mock.assert_not_called()
            add_mock.assert_called_once_with(["token-a", "token-b"], source_type="codex")
            refresh_mock.assert_called_once_with(["token-a", "token-b"])
            job = config.get_import_job(server["id"])
            self.assertEqual(job["status"], "completed")
            self.assertEqual(job["added"], 2)
            self.assertEqual(job["refreshed"], 2)
            self.assertEqual(job["failed"], 0)

    def test_run_import_from_list_tokens_records_missing_accounts_and_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = sub2api_service.Sub2APIConfig(Path(temp_dir) / "sub2api_config.json")
            server = config.add_server(name="", base_url="http://sub2api.test", email="", password="", api_key="key")
            service = sub2api_service.Sub2APIImportService(config)
            config.set_import_job(server["id"], {
                "job_id": "job",
                "status": "pending",
                "created_at": "now",
                "updated_at": "now",
                "total": 3,
                "completed": 0,
                "added": 0,
                "skipped": 0,
                "refreshed": 0,
                "failed": 0,
                "errors": [],
            })

            with mock.patch.object(sub2api_service, "list_remote_accounts", return_value=[
                {"id": "a", "_access_token": "token-a"},
                {"id": "b"},
            ]):
                with mock.patch.object(sub2api_service, "_fetch_access_token_for_account", side_effect=RuntimeError("missing access_token")):
                    with mock.patch.object(sub2api_service.account_service, "add_accounts", return_value={"added": 1, "skipped": 0, "items": []}) as add_mock:
                        with mock.patch.object(sub2api_service.account_service, "refresh_accounts", return_value={"refreshed": 1, "errors": [], "items": []}):
                            service._run_import_from_list_tokens(server["id"], server, ["a", "b", "c"])

            add_mock.assert_called_once_with(["token-a"], source_type="codex")
            job = config.get_import_job(server["id"])
            self.assertEqual(job["status"], "completed")
            self.assertEqual(job["completed"], 3)
            self.assertEqual(job["failed"], 2)
            self.assertEqual(
                {(item["name"], item["error"]) for item in job["errors"]},
                {("b", "missing access_token"), ("c", "account not found")},
            )

    def test_start_import_defaults_to_detail_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = sub2api_service.Sub2APIConfig(Path(temp_dir) / "sub2api_config.json")
            server = config.add_server(name="", base_url="http://sub2api.test", email="", password="", api_key="key")
            service = sub2api_service.Sub2APIImportService(config)
            with mock.patch.object(sub2api_service.threading.Thread, "start"):
                with mock.patch.object(service, "_run_import") as detail_mock:
                    job = service.start_import(server, ["a"])

            self.assertEqual(job["import_method"], "detail")
            self.assertEqual(detail_mock.call_count, 0)
            self.assertEqual(config.get_import_job(server["id"])["import_method"], "detail")


if __name__ == "__main__":
    unittest.main()
