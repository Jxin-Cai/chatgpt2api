from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from services.account_service import AccountService
from services.model_service import (
    ModelCatalogService,
    apply_model_identity_messages,
    model_identity_prompt,
    model_product_name,
    public_model_name,
)
from services.storage.json_storage import JSONStorageBackend


def model_list(*model_ids: str) -> dict:
    return {
        "object": "list",
        "data": [
            {
                "id": model_id,
                "object": "model",
                "created": 0,
                "owned_by": "chatgpt",
                "permission": [],
                "root": model_id,
                "parent": None,
            }
            for model_id in model_ids
        ],
    }


class FakeBackend:
    def __init__(self, access_token: str, outcomes: dict[str, object], calls: list[str], closed: list[str]) -> None:
        self.access_token = access_token
        self._outcomes = outcomes
        self._calls = calls
        self._closed = closed

    def list_models(self) -> dict:
        self._calls.append(self.access_token)
        outcome = self._outcomes[self.access_token]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def close(self) -> None:
        self._closed.append(self.access_token)


class FakeHTTPError(RuntimeError):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


class ModelCatalogServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.accounts = AccountService(
            JSONStorageBackend(Path(self.temp_dir.name) / "accounts.json")
        )
        self.accounts.add_account_items(
            [
                {"access_token": "free-bad", "type": "free", "status": "正常"},
                {"access_token": "free-good", "type": "FREE", "status": "正常"},
                {"access_token": "plus", "type": "Plus", "status": "正常"},
                {"access_token": "pro", "type": "pro", "status": "正常"},
                {"access_token": "team-disabled", "type": "Team", "status": "禁用"},
            ]
        )
        self.accounts.refresh_access_token = lambda token, **_kwargs: token
        self.now = 1000.0
        self.calls: list[str] = []
        self.closed: list[str] = []
        self.outcomes: dict[str, object] = {
            "": model_list("anon", "shared"),
            "free-bad": RuntimeError("expired"),
            "free-good": model_list("free-only", "shared"),
            "plus": model_list("plus-only", "shared"),
            "pro": model_list("pro-only"),
        }
        self.catalog = ModelCatalogService(
            self.accounts,
            backend_factory=lambda access_token="": FakeBackend(
                access_token, self.outcomes, self.calls, self.closed
            ),
            cache_ttl_seconds=300,
            clock=lambda: self.now,
        )

    def test_catalog_unions_each_authenticated_active_account_type(self) -> None:
        result = self.catalog.list_models()

        self.assertEqual(
            [item["id"] for item in result["data"]],
            ["free-only", "plus-only", "pro-only", "shared"],
        )
        self.assertCountEqual(self.calls, ["free-bad", "free-good", "plus", "pro"])
        self.assertCountEqual(self.closed, self.calls)
        self.assertNotIn("team-disabled", self.calls)

        pro_route = self.catalog.route_for_model("pro-only")
        self.assertEqual(pro_route.account_types, frozenset({"Pro"}))
        self.assertFalse(pro_route.allow_anonymous)

        shared_route = self.catalog.route_for_model("shared")
        self.assertEqual(shared_route.account_types, frozenset({"free", "Plus"}))
        self.assertFalse(shared_route.allow_anonymous)

        auto_route = self.catalog.route_for_model("auto")
        self.assertEqual(
            auto_route.account_types,
            frozenset({"free", "Plus", "Pro"}),
        )
        self.assertEqual(auto_route.upstream_model, "auto")
        self.assertFalse(auto_route.allow_anonymous)

    def test_catalog_is_cached_until_ttl_expires(self) -> None:
        self.catalog.list_models()
        self.catalog.list_models()
        self.catalog.route_for_model("pro-only")

        self.assertEqual(self.calls.count("pro"), 1)
        self.assertEqual(self.calls.count(""), 0)

    def test_concurrent_readers_share_one_catalog_refresh(self) -> None:
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(lambda _index: self.catalog.list_models(), range(8)))

        self.assertTrue(all(result == results[0] for result in results))
        self.assertEqual(self.calls.count(""), 0)
        self.assertEqual(self.calls.count("free-good"), 1)
        self.assertEqual(self.calls.count("plus"), 1)
        self.assertEqual(self.calls.count("pro"), 1)

    def test_failed_refresh_keeps_last_successful_models_for_that_type(self) -> None:
        self.catalog.list_models()
        self.outcomes["pro"] = RuntimeError("temporary upstream failure")
        self.now += 301

        result = self.catalog.list_models()

        self.assertIn("pro-only", {item["id"] for item in result["data"]})
        self.assertEqual(
            self.catalog.route_for_model("pro-only").account_types,
            frozenset({"Pro"}),
        )
        self.assertEqual(self.calls.count("pro"), 2)

    def test_unauthorized_model_catalog_excludes_models_without_mutating_account(self) -> None:
        self.outcomes["pro"] = FakeHTTPError(401)

        result = self.catalog.list_models()

        self.assertNotIn("pro-only", {item["id"] for item in result["data"]})
        pro_account = next(
            account
            for account in self.accounts.list_accounts()
            if account["access_token"] == "pro"
        )
        self.assertEqual(pro_account["status"], "正常")
        self.assertEqual(pro_account["quota"], 0)

    def test_unauthorized_refresh_does_not_keep_stale_models(self) -> None:
        self.catalog.list_models()
        self.outcomes["pro"] = FakeHTTPError(401)
        self.now += 301

        result = self.catalog.list_models()

        self.assertNotIn("pro-only", {item["id"] for item in result["data"]})

    def test_removed_account_type_drops_its_stale_capabilities(self) -> None:
        self.catalog.list_models()
        self.accounts.delete_accounts(["pro"])

        result = self.catalog.list_models()

        self.assertNotIn("pro-only", {item["id"] for item in result["data"]})
        self.assertEqual(
            self.catalog.route_for_model("pro-only").account_types,
            frozenset(),
        )

    def test_compatibility_aliases_never_substitute_a_different_model(self) -> None:
        self.outcomes["free-good"] = model_list("gpt-5-6", "gpt-5-6-mini")

        result = self.catalog.list_models()

        models = {item["id"]: item for item in result["data"]}
        self.assertEqual(models["gpt-5.6"]["root"], "gpt-5-6")
        self.assertEqual(models["gpt-5.6-mini"]["root"], "gpt-5-6-mini")
        self.assertNotIn("gpt-5.6-sol", models)
        self.assertNotIn("gpt-5.6-sol-wm", models)
        self.assertNotIn("gpt-5.6-terra", models)
        self.assertNotIn("gpt-5.6-luna", models)
        self.assertNotIn("gpt-5.4", models)

        for requested in ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"):
            with self.subTest(requested=requested):
                route = self.catalog.route_for_model(requested)
                self.assertEqual(route.upstream_model, requested)
                self.assertFalse(route.account_types)
                self.assertFalse(route.allow_anonymous)

    def test_unknown_client_model_is_not_silently_downgraded(self) -> None:
        self.outcomes["free-good"] = model_list("gpt-5-5")

        result = self.catalog.list_models()

        self.assertNotIn("gpt-5.6-sol", {item["id"] for item in result["data"]})
        route = self.catalog.route_for_model("gpt-5.6-sol")
        self.assertEqual(route.upstream_model, "gpt-5.6-sol")
        self.assertFalse(route.allow_anonymous)
        self.assertFalse(route.account_types)

    def test_codex_client_alias_prefers_matching_web_work_mode_slug(self) -> None:
        self.outcomes["plus"] = model_list(
            "gpt-5-6",
            "gpt-5.6-sol-wm",
            "gpt-5.6-terra-wm",
            "gpt-5.6-luna-wm",
            "gpt-6-astra-wm",
        )

        result = self.catalog.list_models()

        models = {item["id"]: item for item in result["data"]}
        self.assertEqual(models["gpt-5.6-sol"]["root"], "gpt-5.6-sol-wm")
        self.assertEqual(models["gpt-5.6-terra"]["root"], "gpt-5.6-terra-wm")
        self.assertEqual(models["gpt-5.6-luna"]["root"], "gpt-5.6-luna-wm")
        self.assertEqual(models["gpt-6-astra"]["root"], "gpt-6-astra-wm")
        self.assertEqual(
            self.catalog.route_for_model("gpt-5.6-sol").upstream_model,
            "gpt-5.6-sol-wm",
        )


class ModelIdentityTests(unittest.TestCase):
    def test_work_mode_slug_becomes_public_family_name(self) -> None:
        self.assertEqual(public_model_name("gpt-5.6-luna-wm"), "gpt-5.6-luna")
        self.assertEqual(public_model_name("gpt-5.6-luna"), "gpt-5.6-luna")
        self.assertEqual(public_model_name("gpt-5.6-sol-wm"), "gpt-5.6-sol")
        self.assertEqual(public_model_name("gpt-6-astra-wm"), "gpt-6-astra")
        self.assertEqual(model_product_name("gpt-5.6-luna-wm"), "GPT-5.6 Luna")
        self.assertEqual(model_product_name("gpt-5.6-sol"), "GPT-5.6 Sol")

    def test_luna_identity_does_not_claim_default_sol(self) -> None:
        prompt = model_identity_prompt("gpt-5.6-luna-wm")

        self.assertIn("GPT-5.6 Luna (gpt-5.6-luna)", prompt)
        self.assertIn("currently using GPT-5.6 Luna (gpt-5.6-luna)", prompt)
        self.assertNotIn("currently using GPT-5.6 Sol", prompt)

    def test_identity_messages_are_prepended_once(self) -> None:
        messages = [{"role": "user", "content": "你是什么模型"}]

        first = apply_model_identity_messages(messages, "gpt-5.6-luna-wm")
        second = apply_model_identity_messages(first, "gpt-5.6-luna-wm")

        self.assertEqual(len(second), 2)
        self.assertEqual(second[0]["role"], "system")
        self.assertIn("gpt-5.6-luna", second[0]["content"])
        self.assertEqual(second[1], messages[0])

    def test_auto_model_does_not_get_identity_prompt(self) -> None:
        messages = [{"role": "user", "content": "hello"}]

        self.assertEqual(model_identity_prompt("auto"), "")
        self.assertEqual(apply_model_identity_messages(messages, "auto"), messages)


if __name__ == "__main__":
    unittest.main()
