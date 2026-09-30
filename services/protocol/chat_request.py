"""Validation shared by the OpenAI compatibility adapters.

Web conversations do not implement every API generation control. Reject those
controls before starting a stream instead of returning an apparently valid but
semantically different completion.
"""
from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from services.model_service import canonical_text_model
from utils.text_models import web_thinking_effort


def invalid_parameter(param: str, message: str, *, unsupported: bool = False) -> None:
    raise HTTPException(status_code=400, detail={"error": {
        "message": message,
        "type": "invalid_request_error",
        "param": param,
        "code": "unsupported_parameter" if unsupported else "invalid_value",
    }})


def normalize_thinking_effort(value: object) -> str:
    if value is None or value == "":
        return ""
    if not isinstance(value, str):
        invalid_parameter("reasoning_effort", "reasoning effort must be a string")
    normalized = value.strip().lower()
    if normalized in {"", "auto", "none", "minimal", "min", "low", "medium", "high", "standard", "extended", "max"}:
        return normalized
    if normalized == "xhigh":
        return "max"
    invalid_parameter("reasoning_effort", f"unsupported reasoning effort {value!r}")


def thinking_effort_from_body(body: dict[str, Any], *, responses: bool = False) -> str:
    reasoning = body.get("reasoning")
    if reasoning is not None and not isinstance(reasoning, dict):
        invalid_parameter("reasoning", "reasoning must be an object")
    fields = [
        ("thinking_effort", body.get("thinking_effort")),
        ("reasoning_effort", body.get("reasoning_effort")),
        ("reasoning.effort", (reasoning or {}).get("effort")),
    ]
    if responses:
        fields = [fields[2], *fields[:2]]
    selected = None
    selected_param = "reasoning_effort"
    for param, value in fields:
        if value is None:
            continue
        try:
            normalized = normalize_thinking_effort(value)
        except HTTPException as exc:
            exc.detail["error"]["param"] = param
            raise
        if selected is None:
            selected = normalized
            selected_param = param
    model = canonical_text_model(str(body.get("model") or ""))
    effort = web_thinking_effort(selected or "")
    if effort and model == "gpt-5.6-instant":
        invalid_parameter(selected_param, "GPT-5.6 Instant does not support a thinking effort", unsupported=True)
    if effort and model == "gpt-6-pro" and effort != "standard":
        invalid_parameter(selected_param, "GPT-6 Pro supports only standard (medium) thinking effort", unsupported=True)
    return selected or ""


def validate_generation_controls(body: dict[str, Any]) -> None:
    for param in (
        "max_tokens", "max_completion_tokens", "temperature", "top_p",
        "frequency_penalty", "presence_penalty", "seed", "logit_bias",
        "top_logprobs", "audio", "prediction", "functions", "function_call",
    ):
        if body.get(param) is not None:
            invalid_parameter(param, f"{param} is not supported by the ChatGPT Web backend", unsupported=True)
    if body.get("stop") not in (None, []):
        invalid_parameter("stop", "stop sequences are not supported by the ChatGPT Web backend", unsupported=True)
    for param in ("logprobs", "store"):
        if body.get(param) not in (None, False):
            invalid_parameter(param, f"{param} is not supported by this backend", unsupported=True)
    if body.get("service_tier") not in (None, "auto"):
        invalid_parameter("service_tier", "API service tiers cannot be selected through ChatGPT Web", unsupported=True)
    if body.get("modalities") not in (None, ["text"]):
        invalid_parameter("modalities", "text chat supports only the text output modality", unsupported=True)
    n = body.get("n")
    if n is not None and (type(n) is not int or n != 1):
        invalid_parameter("n", "text chat supports only n=1", unsupported=True)
    output_format = body.get("response_format")
    if output_format is not None and output_format != {"type": "text"}:
        invalid_parameter("response_format", "JSON mode and strict structured outputs are not supported", unsupported=True)


def validate_messages(messages: list[dict[str, Any]]) -> None:
    for index, message in enumerate(messages):
        param = f"messages[{index}]"
        if not isinstance(message, dict):
            invalid_parameter(param, "each message must be an object")
        role = message.get("role")
        if not isinstance(role, str) or role not in {"system", "developer", "user", "assistant", "tool"}:
            invalid_parameter(f"{param}.role", "unsupported message role")
        content = message.get("content")
        if content is None and role == "assistant" and message.get("tool_calls"):
            continue
        if not isinstance(content, (str, list)):
            invalid_parameter(f"{param}.content", "content must be a string or an array of content parts")
        if role == "tool" and not message.get("tool_call_id"):
            invalid_parameter(f"{param}.tool_call_id", "tool messages require tool_call_id")
        if not isinstance(content, list):
            continue
        for part_index, part in enumerate(content):
            part_param = f"{param}.content[{part_index}]"
            if not isinstance(part, dict):
                invalid_parameter(part_param, "content parts must be objects")
            kind = part.get("type")
            if not isinstance(kind, str):
                invalid_parameter(f"{part_param}.type", "content type must be a string")
            if kind in {"text", "input_text", "output_text"}:
                if not isinstance(part.get("text"), str):
                    invalid_parameter(f"{part_param}.text", "text must be a string")
            elif kind in {"image_url", "input_image", "image"} and role == "user":
                continue
            else:
                invalid_parameter(f"{part_param}.type", f"unsupported content type {kind!r} for {role}", unsupported=True)
