from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from services.account_service import AccountService
from services.model_service import (
    ModelCatalogService,
    ModelUnavailableError,
    model_account_key,
)
from services.storage.json_storage import JSONStorageBackend
from utils.text_models import SUPPORTED_TEXT_MODELS


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
            "": model_list("anon", 'gpt-5-6-instant'),
            "free-bad": RuntimeError("expired"),
            "free-good": model_list('gpt-6-sol-wm', 'gpt-5-6-instant'),
            "plus": model_list('gpt-5-6-thinking', 'gpt-5-6-instant'),
            "pro": model_list('gpt-6-pro', 'gpt-6-astra-wm'),
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
            ["gpt-6-pro", "gpt-5.6-instant", "gpt-5.6-thinking", "gpt-6-astra", "gpt-6-sol"],
        )
        self.assertCountEqual(self.calls, ["free-bad", "free-good", "plus", "pro"])
        self.assertCountEqual(self.closed, self.calls)
        self.assertNotIn("team-disabled", self.calls)

        pro_route = self.catalog.route_for_model("gpt-6-astra")
        self.assertEqual(pro_route.account_types, frozenset({"Pro"}))
        self.assertFalse(pro_route.allow_anonymous)

        shared_route = self.catalog.route_for_model("gpt-5.6-instant")
        self.assertEqual(shared_route.account_types, frozenset({"free", "Plus"}))
        self.assertFalse(shared_route.allow_anonymous)

        with self.assertRaises(ModelUnavailableError):
            self.catalog.route_for_model("auto")

    def test_catalog_exposes_only_six_canonical_models_without_slug_duplicates(self) -> None:
        self.outcomes["plus"] = model_list(
            *SUPPORTED_TEXT_MODELS,
            *(model.replace(".", "-") + "-wm" for model in SUPPORTED_TEXT_MODELS),
            "auto", "gpt-4o", "gpt-5.5", "gpt-5.6-luna-wm", "chat-latest",
        )
        ids = [item["id"] for item in self.catalog.list_models()["data"]]
        self.assertEqual(ids, list(SUPPORTED_TEXT_MODELS))
        self.assertEqual(len(ids), 6)

    def test_catalog_is_cached_until_ttl_expires(self) -> None:
        self.catalog.list_models()
        self.catalog.list_models()
        self.catalog.route_for_model("gpt-6-astra")

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

        self.assertIn("gpt-6-astra", {item["id"] for item in result["data"]})
        self.assertEqual(
            self.catalog.route_for_model("gpt-6-astra").account_types,
            frozenset({"Pro"}),
        )
        self.assertEqual(self.calls.count("pro"), 2)

    def test_unauthorized_model_catalog_excludes_models_without_mutating_account(self) -> None:
        self.outcomes["pro"] = FakeHTTPError(401)

        result = self.catalog.list_models()

        self.assertNotIn("gpt-6-astra", {item["id"] for item in result["data"]})
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

        self.assertNotIn("gpt-6-astra", {item["id"] for item in result["data"]})

    def test_removed_account_type_drops_its_stale_capabilities(self) -> None:
        self.catalog.list_models()
        self.accounts.delete_accounts(["pro"])

        result = self.catalog.list_models()

        self.assertNotIn("gpt-6-astra", {item["id"] for item in result["data"]})
        self.assertEqual(
            self.catalog.route_for_model("gpt-6-astra").account_types,
            frozenset(),
        )

    def test_legacy_models_are_not_listed_or_routable(self) -> None:
        self.outcomes["plus"] = model_list("gpt-5-5", "gpt-5-6", "gpt-5.6-luna-wm", "auto", "chat-latest")
        models = {item["id"] for item in self.catalog.list_models()["data"]}
        for name in ("gpt-5.5", "gpt-5.6", "gpt-5.6-luna", "auto", "chat-latest"):
            with self.subTest(name=name):
                self.assertNotIn(name, models)
                with self.assertRaises(ModelUnavailableError):
                    self.catalog.route_for_model(name)
                with self.assertRaises(ModelUnavailableError):
                    self.catalog.resolve_model(name)

    def test_unavailable_supported_model_is_not_silently_downgraded(self) -> None:
        route = self.catalog.route_for_model("gpt-6.1-sol")
        self.assertEqual(route.upstream_model, "gpt-6.1-sol-wm")
        self.assertFalse(route.account_types)
        self.assertFalse(route.account_keys)

    def test_chat_and_work_aliases_resolve_to_distinct_web_routes(self) -> None:
        self.outcomes["free-good"] = model_list()
        self.outcomes["pro"] = model_list()
        self.outcomes["plus"] = model_list(
            "gpt-5-6",
            "gpt-5-6-thinking",
            "gpt-5-6-instant",
            "gpt-5.6-luna-wm",
            "gpt-6-astra-wm",
        )

        result = self.catalog.list_models()

        models = {item["id"]: item for item in result["data"]}
        self.assertEqual(models["gpt-5.6-thinking"]["root"], "gpt-5-6-thinking")
        self.assertEqual(models["gpt-5.6-instant"]["root"], "gpt-5-6-instant")
        self.assertNotIn("gpt-5.6-luna", models)
        self.assertEqual(models["gpt-6-astra"]["root"], "gpt-6-astra-wm")
        self.assertEqual(
            self.catalog.route_for_model("gpt-5.6-thinking").upstream_model,
            "gpt-5-6-thinking",
        )


    def test_combined_version_and_work_mode_aliases_resolve_to_same_root(self) -> None:
        self.outcomes["plus"] = model_list("gpt-6-1-sol-wm", "gpt-6-sol-wm")
        models = {item["id"]: item for item in self.catalog.list_models()["data"]}
        for name in ("gpt-6.1-sol", "gpt-6-1-sol", "gpt-6.1-sol-wm"):
            self.assertEqual(self.catalog.resolve_model(name), "gpt-6-1-sol-wm")
        self.assertEqual(models["gpt-6.1-sol"]["root"], "gpt-6-1-sol-wm")
        for name, item in models.items():
            self.assertEqual(self.catalog.resolve_model(name), item["root"])
        self.assertNotIn("chat-latest", models)
        with self.assertRaises(ModelUnavailableError):
            self.catalog.route_for_model("chat-latest")

    def test_work_mode_target_wins_over_bare_catalog_name(self) -> None:
        self.outcomes["plus"] = model_list("gpt-6-1-sol-wm", "gpt-6.1-sol")
        self.assertEqual(self.catalog.resolve_model("gpt-6.1-sol"), "gpt-6-1-sol-wm")
        self.assertEqual(self.catalog.resolve_model("gpt-6.1-sol-wm"), "gpt-6-1-sol-wm")

    def test_chat_pro_uses_native_slug_and_independent_account_permissions(self) -> None:
        self.outcomes["plus"] = model_list("gpt-6-astra-wm", "gpt-6-pro-wm")
        self.outcomes["pro"] = model_list("gpt-6-pro")
        models = {item["id"]: item for item in self.catalog.list_models()["data"]}
        self.assertEqual(models["gpt-6-pro"]["root"], "gpt-6-pro")
        self.assertEqual(self.catalog.resolve_model("gpt-6-pro"), "gpt-6-pro")
        self.assertEqual(
            self.catalog.route_for_model("gpt-6-pro").account_keys,
            frozenset({model_account_key("pro")}),
        )
        with self.assertRaises(ModelUnavailableError):
            self.catalog.resolve_model("gpt-6-pro-wm")

    def test_work_astra_does_not_grant_chat_pro_access(self) -> None:
        self.outcomes["pro"] = model_list("gpt-6-astra-wm", "gpt-6-pro-wm")
        self.assertNotIn("gpt-6-pro", {item["id"] for item in self.catalog.list_models()["data"]})
        with self.assertRaises(ModelUnavailableError):
            self.catalog.resolve_model("gpt-6-pro")

    def test_chat_instant_and_thinking_preserve_native_non_work_routes(self) -> None:
        for name in ("instant", "thinking"):
            for model in (f"gpt-5.6-{name}", f"gpt-5-6-{name}"):
                self.assertEqual(self.catalog.resolve_model(model), f"gpt-5-6-{name}")
            with self.assertRaises(ModelUnavailableError):
                self.catalog.resolve_model(f"gpt-5.6-{name}-wm")

    def test_catalog_rejects_slugs_with_conflicting_mode_metadata(self) -> None:
        self.outcomes["pro"] = {"data": [
            {"id": "gpt-6-pro", "is_work_mode_model": True},
            {"id": "gpt-6-astra-wm", "is_work_mode_model": False},
        ]}
        models = {item["id"] for item in self.catalog.list_models()["data"]}
        self.assertNotIn("gpt-6-pro", models)
        self.assertNotIn("gpt-6-astra", models)

    def test_bare_model_without_work_mode_target_is_not_exposed_or_forwarded(self) -> None:
        self.outcomes["plus"] = model_list("gpt-6.1-sol", "gpt-6-1-sol")
        models = {item["id"] for item in self.catalog.list_models()["data"]}
        self.assertNotIn("gpt-6.1-sol", models)
        self.assertFalse(self.catalog.route_for_model("gpt-6.1-sol").account_keys)
        for model in ("gpt-6.1-sol", "gpt-6.1-sol-wm"):
            with self.subTest(model=model), self.assertRaises(ModelUnavailableError):
                self.catalog.resolve_model(model)

    def test_route_selects_only_account_advertising_work_mode_target(self) -> None:
        self.outcomes["plus"] = model_list("gpt-6.1-sol")
        self.outcomes["pro"] = model_list("gpt-6.1-sol-wm")
        route = self.catalog.route_for_model("gpt-6.1-sol")
        self.assertEqual(route.upstream_model, "gpt-6.1-sol-wm")
        self.assertEqual(route.account_keys, frozenset({model_account_key("pro")}))

    def test_same_plan_accounts_have_independent_model_permissions(self) -> None:
        self.accounts.add_account_items([{"access_token": "plus-new", "type": "Plus"}])
        self.outcomes["plus-new"] = model_list("gpt-6.1-sol-wm")
        self.assertEqual(
            self.catalog.route_for_model("gpt-6.1-sol").account_keys,
            frozenset({model_account_key("plus-new")}),
        )
        self.assertEqual(
            self.catalog.route_for_model("gpt-5.6-thinking").account_keys,
            frozenset({model_account_key("plus")}),
        )

    def test_replacing_account_with_same_plan_and_count_invalidates_cache(self) -> None:
        self.catalog.list_models()
        self.accounts.delete_accounts(["plus"])
        self.accounts.add_account_items([{"access_token": "plus-new", "type": "Plus"}])
        self.outcomes["plus-new"] = model_list('gpt-6-sol-wm')
        ids = {item["id"] for item in self.catalog.list_models()["data"]}
        self.assertNotIn("gpt-5.6-thinking", ids)
        self.assertIn("gpt-6-sol", ids)

    def test_stale_catalog_expires_during_repeated_failures(self) -> None:
        self.catalog.list_models()
        self.outcomes["pro"] = RuntimeError("offline")
        self.now += 901
        self.assertFalse(self.catalog.route_for_model("gpt-6-astra").account_keys)

    def test_forbidden_catalog_revokes_stale_capabilities(self) -> None:
        self.catalog.list_models()
        self.outcomes["pro"] = FakeHTTPError(403)
        self.now += 301
        self.assertFalse(self.catalog.route_for_model("gpt-6-astra").account_keys)


if __name__ == "__main__":
    unittest.main()
