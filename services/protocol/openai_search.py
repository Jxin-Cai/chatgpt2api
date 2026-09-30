from __future__ import annotations

from services.account_service import account_service
from services.model_service import model_catalog_service
from services.openai_backend_api import OpenAIBackendAPI
from utils.text_models import DEFAULT_TEXT_MODEL

MODEL = DEFAULT_TEXT_MODEL


def handle(body: dict[str, object]) -> dict[str, object]:
    token = account_service.get_text_access_token(model=MODEL)
    account = account_service.get_account(token) or {}
    backend = OpenAIBackendAPI(token)
    try:
        result = backend.search(str(body["prompt"]), model=model_catalog_service.resolve_model(MODEL))
    finally:
        backend.close()
    account_service.mark_text_used(token)
    result["_account_email"] = str(account.get("email") or "")
    return result
