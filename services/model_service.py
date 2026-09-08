from __future__ import annotations

import re
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from threading import RLock
from typing import Any

from services.account_service import AccountService, account_service
from services.openai_backend_api import OpenAIBackendAPI
from utils.log import logger


@dataclass(frozen=True)
class ModelRoute:
    account_types: frozenset[str]
    allow_anonymous: bool = False
    upstream_model: str = ""


class ModelUnavailableError(RuntimeError):
    pass


# Codex clients expose dotted product-facing names while ChatGPT Web may use
# hyphenated slugs and a ``-wm`` suffix. Only syntax-equivalent aliases are
# allowed: substituting a different model would silently run the request on the
# wrong model (usually the account's default Sol model).
_DOTTED_MODEL_RE = re.compile(r"^(gpt-\d+)\.(\d+)(.*)$")
_WEB_MODEL_RE = re.compile(r"^gpt-(\d+)-(\d+)(.*)$")
_WORK_MODE_MODEL_RE = re.compile(r"^gpt-\d+(?:\.\d+)?-(?:sol|terra|luna|astra)-wm$")
_CLIENT_WORK_MODE_ALIAS_RE = re.compile(r"^gpt-\d+(?:\.\d+)?-(?:sol|terra|luna|astra)$")


def _model_alias_candidates(model: str) -> tuple[str, ...]:
    normalized = str(model or "").strip().lower()
    candidates = []
    if _CLIENT_WORK_MODE_ALIAS_RE.fullmatch(normalized):
        candidates.append(f"{normalized}-wm")
    dotted = _DOTTED_MODEL_RE.fullmatch(normalized)
    if dotted:
        candidates.append(f"{dotted.group(1)}-{dotted.group(2)}{dotted.group(3)}")
    return tuple(dict.fromkeys(candidate for candidate in candidates if candidate != normalized))


def _resolve_model_alias(model: str, available_models: set[str]) -> str:
    requested = str(model or "").strip() or "auto"
    normalized = requested.lower()
    if normalized in available_models:
        return normalized
    for candidate in _model_alias_candidates(normalized):
        if candidate in available_models:
            return candidate
    return requested


def _available_model_aliases(available_models: set[str]) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for upstream_model in sorted(available_models):
        if _WORK_MODE_MODEL_RE.fullmatch(upstream_model):
            work_mode_alias = upstream_model[:-3]
            if work_mode_alias not in available_models:
                aliases[work_mode_alias] = upstream_model
        match = _WEB_MODEL_RE.fullmatch(upstream_model)
        if not match:
            continue
        alias = f"gpt-{match.group(1)}.{match.group(2)}{match.group(3)}"
        if alias not in available_models:
            aliases[alias] = upstream_model
    return aliases


class ModelCatalogService:
    """Caches the model catalogs advertised to each active account type."""

    def __init__(
        self,
        accounts: AccountService,
        *,
        backend_factory: Callable[..., Any] = OpenAIBackendAPI,
        cache_ttl_seconds: float = 300,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._accounts = accounts
        self._backend_factory = backend_factory
        self._cache_ttl_seconds = max(1.0, float(cache_ttl_seconds))
        self._clock = clock
        self._lock = RLock()
        self._expires_at = 0.0
        self._account_signature: tuple[tuple[str, int], ...] = ()
        self._anonymous_models: dict[str, dict[str, Any]] = {}
        self._models_by_account_type: dict[str, dict[str, dict[str, Any]]] = {}

    @staticmethod
    def _model_map(result: object) -> dict[str, dict[str, Any]]:
        if not isinstance(result, dict) or not isinstance(result.get("data"), list):
            raise TypeError("upstream model response has no data list")
        models: dict[str, dict[str, Any]] = {}
        for item in result["data"]:
            if not isinstance(item, dict):
                continue
            model_id = str(item.get("id") or "").strip()
            if model_id and model_id not in models:
                models[model_id] = dict(item)
        return models

    def _active_accounts_by_type(self) -> dict[str, list[str]]:
        groups: dict[str, list[str]] = {}
        for account in self._accounts.list_accounts():
            if not isinstance(account, dict) or account.get("status") in {"禁用", "异常"}:
                continue
            access_token = str(account.get("access_token") or "").strip()
            account_type = self._accounts._normalize_account_type(account.get("type"))
            if access_token and account_type:
                groups.setdefault(account_type, []).append(access_token)
        return groups

    @staticmethod
    def _signature(groups: dict[str, list[str]]) -> tuple[tuple[str, int], ...]:
        return tuple(
            (account_type, len(tokens))
            for account_type, tokens in sorted(groups.items())
        )

    def _fetch_models(self, access_token: str = "") -> dict[str, dict[str, Any]]:
        backend = self._backend_factory(access_token=access_token)
        try:
            return self._model_map(backend.list_models())
        finally:
            backend.close()

    def _fetch_account_type_models(
        self,
        account_type: str,
        access_tokens: list[str],
    ) -> dict[str, dict[str, Any]] | None:
        attempted_tokens: set[str] = set()
        last_error: Exception | None = None
        unauthorized_seen = False
        for access_token in access_tokens:
            resolved_token = access_token
            try:
                resolved_token = self._accounts.refresh_access_token(
                    access_token,
                    event="list_models",
                ) or access_token
                if resolved_token in attempted_tokens:
                    continue
                attempted_tokens.add(resolved_token)
                return self._fetch_models(resolved_token)
            except Exception as exc:  # noqa: BLE001 - try the next account for any upstream failure
                if getattr(exc, "status_code", None) == 401:
                    refreshed_token = self._accounts.refresh_access_token(
                        resolved_token,
                        force=True,
                        event="list_models:unauthorized",
                    ) or resolved_token
                    if refreshed_token not in attempted_tokens:
                        attempted_tokens.add(refreshed_token)
                        try:
                            return self._fetch_models(refreshed_token)
                        except Exception as retry_exc:  # noqa: BLE001 - try the next account
                            exc = retry_exc
                    unauthorized_seen = getattr(exc, "status_code", None) == 401
                last_error = exc
        if last_error is not None:
            logger.warning({
                "event": "model_catalog_account_type_failed",
                "account_type": account_type,
                "error_type": type(last_error).__name__,
            })
        if unauthorized_seen:
            return {}
        return None

    def _refresh(self, groups: dict[str, list[str]], signature: tuple[tuple[str, int], ...]) -> None:
        models_by_account_type: dict[str, dict[str, dict[str, Any]]] = {}
        with ThreadPoolExecutor(max_workers=max(1, min(4, len(groups)))) as executor:
            account_futures = {
                account_type: executor.submit(
                    self._fetch_account_type_models,
                    account_type,
                    access_tokens,
                )
                for account_type, access_tokens in groups.items()
            }
            for account_type, future in account_futures.items():
                models = future.result()
                if models is not None:
                    models_by_account_type[account_type] = models
                elif account_type in self._models_by_account_type:
                    models_by_account_type[account_type] = self._models_by_account_type[account_type]

        # The anonymous models endpoint may enumerate models even when the
        # anonymous conversation endpoint is blocked.  It is therefore not a
        # reliable source for an API that promises callable models.
        self._anonymous_models = {}
        self._models_by_account_type = models_by_account_type
        self._account_signature = signature
        self._expires_at = self._clock() + self._cache_ttl_seconds

    def _ensure_catalog(self) -> None:
        groups = self._active_accounts_by_type()
        signature = self._signature(groups)
        with self._lock:
            if signature == self._account_signature and self._clock() < self._expires_at:
                return
            self._refresh(groups, signature)

    def list_models(self) -> dict[str, Any]:
        self._ensure_catalog()
        with self._lock:
            union: dict[str, dict[str, Any]] = {
                model_id: dict(item)
                for model_id, item in self._anonymous_models.items()
            }
            for account_type in sorted(self._models_by_account_type):
                for model_id, item in self._models_by_account_type[account_type].items():
                    union.setdefault(model_id, dict(item))
            aliases = _available_model_aliases(set(union))
            for alias, upstream_model in aliases.items():
                item = dict(union[upstream_model])
                item.update({
                    "id": alias,
                    "root": upstream_model,
                    "parent": None,
                })
                union[alias] = item
        return {
            "object": "list",
            "data": [union[model_id] for model_id in sorted(union)],
        }

    def resolve_model(self, model: str) -> str:
        """Resolve a public compatibility name to a live ChatGPT Web slug."""
        self._ensure_catalog()
        with self._lock:
            available_models = set(self._anonymous_models)
            for models in self._models_by_account_type.values():
                available_models.update(models)
            return _resolve_model_alias(model, available_models)

    def route_for_model(self, model: str) -> ModelRoute:
        self._ensure_catalog()
        with self._lock:
            available_models = set(self._anonymous_models)
            for models in self._models_by_account_type.values():
                available_models.update(models)
            upstream_model = _resolve_model_alias(model, available_models)
            if upstream_model.lower() == "auto":
                return ModelRoute(
                    account_types=frozenset(
                        account_type
                        for account_type, models in self._models_by_account_type.items()
                        if models
                    ),
                    allow_anonymous=bool(self._anonymous_models),
                    upstream_model="auto",
                )
            account_types = frozenset(
                account_type
                for account_type, models in self._models_by_account_type.items()
                if upstream_model in models
            )
            return ModelRoute(
                account_types=account_types,
                allow_anonymous=upstream_model in self._anonymous_models,
                upstream_model=upstream_model,
            )


model_catalog_service = ModelCatalogService(account_service)
