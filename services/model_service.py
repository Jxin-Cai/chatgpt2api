from __future__ import annotations

import hashlib
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
from utils.text_models import CHAT_TEXT_MODELS, DEFAULT_TEXT_MODEL, SUPPORTED_TEXT_MODELS


@dataclass(frozen=True)
class ModelRoute:
    account_types: frozenset[str]
    allow_anonymous: bool = False
    upstream_model: str = ""
    # Fingerprints, never bearer tokens. None preserves callers supplying a
    # plan-only route; catalog routes always constrain individual accounts.
    account_keys: frozenset[str] | None = None


class ModelUnavailableError(RuntimeError):
    status_code = 404

    def to_openai_error(self) -> dict[str, Any]:
        return {"error": {
            "message": str(self), "type": "invalid_request_error",
            "param": "model", "code": "model_not_found",
        }}


def model_account_key(access_token: str) -> str:
    return hashlib.sha256(access_token.encode("utf-8")).hexdigest()


_DOTTED_MODEL_RE = re.compile(r"^(gpt-\d+)\.(\d+)(.*)$")
_WEB_MODEL_RE = re.compile(r"^gpt-(\d+)-(\d+)(.*)$")
_WORK_MODE_MODEL_RE = re.compile(r"^gpt-\d+(?:[.-]\d+)?-(?:sol|terra|luna|astra)$")


def canonical_text_model(model: str) -> str:
    value = str(model or "").strip().lower().removesuffix("-wm")
    if match := _WEB_MODEL_RE.fullmatch(value):
        value = f"gpt-{match[1]}.{match[2]}{match[3]}"
    return value


def require_supported_text_model(model: object = None) -> str:
    value = canonical_text_model(str(model or DEFAULT_TEXT_MODEL))
    if value not in SUPPORTED_TEXT_MODELS or (
        value in CHAT_TEXT_MODELS and str(model or "").strip().lower().endswith("-wm")
    ):
        raise ModelUnavailableError(f"model {model!r} is not supported; select a model from /v1/models")
    return value


def _model_alias_candidates(model: str) -> tuple[str, ...]:
    """Compose spelling aliases without guessing a different model family."""
    normalized = str(model or "").strip().lower()
    spellings = [normalized]
    if match := _DOTTED_MODEL_RE.fullmatch(normalized):
        spellings.append(f"{match[1]}-{match[2]}{match[3]}")
    elif match := _WEB_MODEL_RE.fullmatch(normalized):
        spellings.append(f"gpt-{match[1]}.{match[2]}{match[3]}")
    candidates = list(spellings)
    for spelling in spellings:
        base = spelling.removesuffix("-wm")
        if _WORK_MODE_MODEL_RE.fullmatch(base):
            candidates.append(base if spelling.endswith("-wm") else f"{base}-wm")
    return tuple(dict.fromkeys(value for value in candidates if value != normalized))


def _resolve_model_alias(model: str, available_models: set[str]) -> str:
    public_model = require_supported_text_model(model)
    work_mode = public_model not in CHAT_TEXT_MODELS
    requested = f"{public_model}-wm" if work_mode else public_model
    for candidate in (requested, *_model_alias_candidates(requested)):
        if candidate.endswith("-wm") != work_mode:
            continue
        if candidate in available_models:
            return candidate
    return requested


class ModelCatalogService:
    """Cache each account's Web catalog; plans do not guarantee model access."""

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
        self._account_signature: tuple[tuple[str, str], ...] = ()
        self._models_by_account: dict[str, dict[str, dict[str, Any]]] = {}
        self._account_types: dict[str, str] = {}
        self._last_success: dict[str, float] = {}

    @staticmethod
    def _model_map(result: object) -> dict[str, dict[str, Any]]:
        if not isinstance(result, dict) or not isinstance(result.get("data"), list):
            raise TypeError("upstream model response has no data list")
        models: dict[str, dict[str, Any]] = {}
        for item in result["data"]:
            if isinstance(item, dict) and (model_id := str(item.get("id") or "").strip()):
                public_model = canonical_text_model(model_id)
                work_mode = public_model not in CHAT_TEXT_MODELS
                if (
                    public_model in SUPPORTED_TEXT_MODELS
                    and model_id.endswith("-wm") == work_mode
                    and ("is_work_mode_model" not in item or item["is_work_mode_model"] == work_mode)
                ):
                    models.setdefault(model_id, dict(item))
        return models

    def _active_accounts(self) -> dict[str, tuple[str, str]]:
        accounts = {}
        for account in self._accounts.list_accounts():
            if not isinstance(account, dict) or account.get("status") in {"禁用", "异常"}:
                continue
            token = str(account.get("access_token") or "").strip()
            kind = self._accounts._normalize_account_type(account.get("type"))
            if token and kind:
                accounts[model_account_key(token)] = (kind, token)
        return accounts

    @staticmethod
    def _signature(accounts: dict[str, tuple[str, str]]) -> tuple[tuple[str, str], ...]:
        return tuple(sorted((key, kind) for key, (kind, _token) in accounts.items()))

    def _fetch_models(self, access_token: str) -> dict[str, dict[str, Any]]:
        backend = self._backend_factory(access_token=access_token)
        try:
            return self._model_map(backend.list_models())
        finally:
            backend.close()

    def _fetch_account_models(self, token: str) -> tuple[str, dict[str, dict[str, Any]] | None]:
        resolved = token
        try:
            resolved = self._accounts.refresh_access_token(token, event="list_models") or token
            try:
                return resolved, self._fetch_models(resolved)
            except Exception as exc:
                if getattr(exc, "status_code", None) != 401:
                    raise
                refreshed = self._accounts.refresh_access_token(
                    resolved, force=True, event="list_models:unauthorized",
                ) or resolved
                if refreshed == resolved:
                    raise
                resolved = refreshed
                return resolved, self._fetch_models(resolved)
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            logger.warning({"event": "model_catalog_account_failed", "error_type": type(exc).__name__})
            # Authorization failures revoke old capabilities immediately.
            return resolved, {} if status in {401, 403} else None

    def _refresh(self, accounts: dict[str, tuple[str, str]]) -> None:
        models_by_account = {}
        account_types = {}
        last_success = {}
        with ThreadPoolExecutor(max_workers=max(1, min(4, len(accounts)))) as executor:
            futures = {
                key: executor.submit(self._fetch_account_models, token)
                for key, (_kind, token) in accounts.items()
            }
            for old_key, future in futures.items():
                token, models = future.result()
                key = model_account_key(token)
                kind = accounts[old_key][0]
                if models is not None:
                    models_by_account[key] = models
                    last_success[key] = self._clock()
                elif (
                    key == old_key and key in self._models_by_account
                    and self._clock() - self._last_success.get(key, 0) < 3 * self._cache_ttl_seconds
                ):
                    # Brief outages may use a bounded stale catalog, only for
                    # the same credential. Never inherit another account's access.
                    models_by_account[key] = self._models_by_account[key]
                    last_success[key] = self._last_success[key]
                account_types[key] = kind
        self._models_by_account = models_by_account
        self._account_types = account_types
        self._last_success = last_success
        # Refresh may rotate credentials; record the resulting pool signature.
        self._account_signature = tuple(sorted(account_types.items()))
        self._expires_at = self._clock() + self._cache_ttl_seconds

    def _ensure_catalog(self) -> None:
        with self._lock:
            accounts = self._active_accounts()
            if self._signature(accounts) == self._account_signature and self._clock() < self._expires_at:
                return
            self._refresh(accounts)

    def _available_models(self) -> set[str]:
        return {model for models in self._models_by_account.values() for model in models}

    def list_models(self) -> dict[str, Any]:
        self._ensure_catalog()
        with self._lock:
            union: dict[str, dict[str, Any]] = {}
            for key in sorted(self._models_by_account):
                for model_id, item in self._models_by_account[key].items():
                    union.setdefault(model_id, dict(item))
            data = []
            for model in SUPPORTED_TEXT_MODELS:
                upstream_model = _resolve_model_alias(model, set(union))
                if upstream_model in union:
                    data.append({**union[upstream_model], "id": model, "root": upstream_model, "parent": None})
        return {"object": "list", "data": data}

    def resolve_model(self, model: str) -> str:
        model = require_supported_text_model(model)
        self._ensure_catalog()
        with self._lock:
            available_models = self._available_models()
            upstream_model = _resolve_model_alias(model, available_models)
            if upstream_model not in available_models:
                raise ModelUnavailableError(f"no active account advertises the required Chat/Work target for {model!r}")
            return upstream_model

    def route_for_model(self, model: str) -> ModelRoute:
        model = require_supported_text_model(model)
        self._ensure_catalog()
        with self._lock:
            upstream_model = _resolve_model_alias(model, self._available_models())
            keys = frozenset(
                key for key, models in self._models_by_account.items()
                if upstream_model in models
            )
            return ModelRoute(
                account_types=frozenset(self._account_types[key] for key in keys),
                upstream_model=upstream_model,
                account_keys=keys,
            )


model_catalog_service = ModelCatalogService(account_service)
