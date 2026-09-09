from __future__ import annotations

import time
import uuid
from typing import Any, Iterable, Iterator

from fastapi import HTTPException

from services.protocol.chat_completion_cache import cache_key, chat_completion_cache, normalize_text_messages
from services.protocol.conversation import (
    ConversationRequest,
    ImageOutput,
    collect_image_outputs,
    collect_text_output,
    count_message_image_tokens,
    count_message_text_tokens,
    count_text_tokens,
    encode_images,
    normalize_messages,
    stream_image_outputs_with_pool,
    stream_text_parts,
    text_backend,
)
from services.protocol.function_tool_bridge import (
    FUNCTION_TOOL_TYPE,
    build_function_tool_prompt,
    function_tools,
    has_function_call_syntax,
    has_function_tools,
    parsed_function_calls,
    preprocess_function_tool_messages,
    strip_function_tool_markup,
)
from services.protocol.web_search_tool import (
    WEB_SEARCH_TOOL_TYPES,
    has_unsupported_tools,
    is_web_search_chat_request,
    run_web_search,
    search_prompt_from_messages,
    text_with_url_citations,
    web_search_options,
)
from utils.helper import (
    DEFAULT_IMAGE_MODEL,
    build_chat_image_markdown_content,
    extract_chat_image,
    extract_chat_prompt,
    is_image_chat_request,
    parse_image_count,
)
from utils.image_tokens import (
    chat_usage_from_image_usage,
    count_image_inputs_tokens,
    count_image_output_items_tokens,
    image_usage,
)

TOOL_UNAVAILABLE_SYSTEM_MESSAGE = (
    "This compatibility backend cannot execute unknown built-in tools, shell commands, "
    "or file operations. Do not claim to have run tools or inspected external resources. "
    "If a user asks you to use a tool, say that tool execution is unavailable through this backend."
)
SUPPORTED_CHAT_TOOL_TYPES = WEB_SEARCH_TOOL_TYPES | {FUNCTION_TOOL_TYPE}


def normalize_thinking_effort(value: object) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"", "none", "auto"}:
        return ""
    if normalized in {"minimal", "low", "medium", "high", "standard"}:
        return normalized
    if normalized in {"xhigh", "extended", "max"}:
        return "extended"
    return ""


def thinking_effort_from_body(body: dict[str, Any]) -> str:
    if body.get("thinking_effort") is not None:
        return normalize_thinking_effort(body.get("thinking_effort"))
    if body.get("reasoning_effort") is not None:
        return normalize_thinking_effort(body.get("reasoning_effort"))
    reasoning = body.get("reasoning")
    if isinstance(reasoning, dict):
        return normalize_thinking_effort(reasoning.get("effort"))
    return ""


def completion_chunk(model: str, delta: dict[str, Any], finish_reason: str | None = None, completion_id: str = "", created: int | None = None) -> dict[str, Any]:
    return {
        "id": completion_id or f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion.chunk",
        "created": created or int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }


def completion_response(
    model: str,
    content: str | None,
    created: int | None = None,
    messages: list[dict[str, Any]] | None = None,
    annotations: list[dict[str, Any]] | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
    raw_completion_text: str = "",
    reasoning_content: str = "",
) -> dict[str, Any]:
    prompt_text_tokens = count_message_text_tokens(messages, model) if messages else 0
    prompt_image_tokens = count_message_image_tokens(messages, model) if messages else 0
    prompt_tokens = prompt_text_tokens + prompt_image_tokens
    completion_text = (raw_completion_text or str(content or "")) + reasoning_content
    completion_tokens = count_text_tokens(completion_text, model) if messages else 0
    message = {"role": "assistant", "content": content}
    if reasoning_content:
        message["reasoning_content"] = reasoning_content
    if annotations:
        message["annotations"] = annotations
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": created or int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": "tool_calls" if tool_calls else "stop",
        }],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "prompt_tokens_details": {
                "text_tokens": prompt_text_tokens,
                "image_tokens": prompt_image_tokens,
                "cached_tokens": 0,
            },
            "completion_tokens_details": {
                "text_tokens": completion_tokens,
                "image_tokens": 0,
                "reasoning_tokens": 0,
            },
        },
    }


def stream_text_chat_completion(
    backend,
    messages: list[dict[str, Any]],
    model: str,
    thinking_effort: str = "",
) -> Iterator[dict[str, Any]]:
    completion_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())
    sent_role = False
    request = ConversationRequest(model=model, messages=messages, thinking_effort=thinking_effort)
    for kind, delta_text in stream_text_parts(backend, request):
        if not sent_role:
            sent_role = True
            yield completion_chunk(model, {"role": "assistant", "content": ""}, None, completion_id, created)
        if kind == "reasoning":
            yield completion_chunk(model, {"reasoning_content": delta_text}, None, completion_id, created)
        else:
            yield completion_chunk(model, {"content": delta_text}, None, completion_id, created)
    if not sent_role:
        yield completion_chunk(model, {"role": "assistant", "content": ""}, None, completion_id, created)
    yield completion_chunk(model, {}, "stop", completion_id, created)


def collect_chat_content(chunks: Iterable[dict[str, Any]]) -> str:
    parts: list[str] = []
    for chunk in chunks:
        choices = chunk.get("choices")
        first = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
        delta = first.get("delta") if isinstance(first.get("delta"), dict) else {}
        content = str(delta.get("content") or "")
        if content:
            parts.append(content)
    return "".join(parts)


def chat_messages_from_body(body: dict[str, Any]) -> list[dict[str, Any]]:
    messages = body.get("messages")
    if isinstance(messages, list) and messages:
        return [message for message in messages if isinstance(message, dict)]
    prompt = str(body.get("prompt") or "").strip()
    if prompt:
        return [{"role": "user", "content": prompt}]
    raise HTTPException(status_code=400, detail={"error": "messages or prompt is required"})


def chat_image_args(body: dict[str, Any]) -> tuple[str, str, int, list[tuple[bytes, str, str]]]:
    model = str(body.get("model") or DEFAULT_IMAGE_MODEL).strip() or DEFAULT_IMAGE_MODEL
    prompt = extract_chat_prompt(body)
    if not prompt:
        raise HTTPException(status_code=400, detail={"error": "prompt is required"})
    images = [
        (data, f"image_{idx}.png", mime)
        for idx, (data, mime) in enumerate(extract_chat_image(body), start=1)
    ]
    return model, prompt, parse_image_count(body.get("n")), images


def text_chat_parts(body: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    model = str(body.get("model") or "auto").strip() or "auto"
    raw_messages = preprocess_function_tool_messages(chat_messages_from_body(body))
    messages = normalize_text_messages(normalize_messages(raw_messages))
    try:
        function_prompt = build_function_tool_prompt(body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"error": str(exc)}) from exc
    if function_prompt:
        messages.insert(0, {"role": "system", "content": function_prompt})
    if has_unsupported_tools(body, SUPPORTED_CHAT_TOOL_TYPES):
        messages.insert(0, {"role": "system", "content": TOOL_UNAVAILABLE_SYSTEM_MESSAGE})
    return model, messages


def chat_completion_annotations(annotations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output = []
    for item in annotations:
        if item.get("type") != "url_citation":
            continue
        output.append({
            "type": "url_citation",
            "url_citation": {
                "start_index": item.get("start_index", 0),
                "end_index": item.get("end_index", 0),
                "url": item.get("url", ""),
                "title": item.get("title", ""),
            },
        })
    return output


def _web_search_result(
    body: dict[str, Any],
    messages: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]], str]:
    query = search_prompt_from_messages(messages, web_search_options(body))
    if not query:
        raise HTTPException(status_code=400, detail={"error": "messages or prompt is required for web search"})
    result = run_web_search(query)
    text, annotations = text_with_url_citations(result)
    return text, annotations, str(result.get("reasoning_content") or "")


def web_search_chat_response(body: dict[str, Any], messages: list[dict[str, Any]], model: str) -> dict[str, Any]:
    text, annotations, reasoning_content = _web_search_result(body, messages)
    return completion_response(
        model,
        text,
        messages=messages,
        annotations=chat_completion_annotations(annotations),
        reasoning_content=reasoning_content,
    )


def _text_chunks(text: str, chunk_size: int = 512) -> Iterator[str]:
    for start in range(0, len(text), chunk_size):
        yield text[start:start + chunk_size]


def stream_web_search_chat_completion(
    body: dict[str, Any],
    messages: list[dict[str, Any]],
    model: str,
) -> Iterator[dict[str, Any]]:
    completion_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())
    # Emit a standard role chunk before the Web search's asynchronous wait so
    # clients do not see a silent stream for one or two minutes.
    yield completion_chunk(model, {"role": "assistant", "content": ""}, None, completion_id, created)
    text, annotations, reasoning_content = _web_search_result(body, messages)
    if reasoning_content:
        yield completion_chunk(
            model,
            {"reasoning_content": reasoning_content},
            None,
            completion_id,
            created,
        )
    for part in _text_chunks(text):
        yield completion_chunk(model, {"content": part}, None, completion_id, created)
    normalized_annotations = chat_completion_annotations(annotations)
    if normalized_annotations:
        yield completion_chunk(model, {"annotations": normalized_annotations}, None, completion_id, created)
    yield completion_chunk(model, {}, "stop", completion_id, created)


def _openai_tool_calls(text: str, body: dict[str, Any]) -> list[dict[str, Any]]:
    parsed = parsed_function_calls(text, function_tools(body))
    choice = body.get("tool_choice")
    forced_function = choice.get("function") if isinstance(choice, dict) and choice.get("type") == "function" else {}
    forced_name = str(forced_function.get("name") or "").strip() if isinstance(forced_function, dict) else ""
    if forced_name and any(name != forced_name for name, _arguments in parsed):
        raise RuntimeError(f"model returned a function other than required {forced_name!r}")
    if (choice == "required" or forced_name) and not parsed:
        raise RuntimeError("model did not return the required function call")
    if body.get("parallel_tool_calls", True) is False:
        parsed = parsed[:1]
    return [
        {
            "id": f"call_{uuid.uuid4().hex}",
            "type": "function",
            "function": {"name": name, "arguments": arguments},
        }
        for name, arguments in parsed
    ]


def function_tool_chat_response(
    body: dict[str, Any],
    messages: list[dict[str, Any]],
    model: str,
    thinking_effort: str = "",
) -> dict[str, Any]:
    output = collect_text_output(
        text_backend(model),
        ConversationRequest(model=model, messages=messages, thinking_effort=thinking_effort),
    )
    raw_text = output.content
    tool_calls = _openai_tool_calls(raw_text, body)
    if has_function_call_syntax(raw_text) and not tool_calls:
        raise RuntimeError("model returned an invalid or unknown function call")
    visible_text = strip_function_tool_markup(raw_text)
    return completion_response(
        model,
        visible_text or (None if tool_calls else raw_text),
        messages=messages,
        tool_calls=tool_calls,
        raw_completion_text=raw_text,
        reasoning_content=output.reasoning_content,
    )


def stream_function_tool_chat_completion(
    body: dict[str, Any],
    messages: list[dict[str, Any]],
    model: str,
    thinking_effort: str = "",
) -> Iterator[dict[str, Any]]:
    completion_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())
    yield completion_chunk(model, {"role": "assistant", "content": None}, None, completion_id, created)
    output = collect_text_output(
        text_backend(model),
        ConversationRequest(model=model, messages=messages, thinking_effort=thinking_effort),
    )
    raw_text = output.content
    if output.reasoning_content:
        yield completion_chunk(
            model,
            {"reasoning_content": output.reasoning_content},
            None,
            completion_id,
            created,
        )
    tool_calls = _openai_tool_calls(raw_text, body)
    if has_function_call_syntax(raw_text) and not tool_calls:
        raise RuntimeError("model returned an invalid or unknown function call")
    visible_text = strip_function_tool_markup(raw_text)
    if visible_text:
        yield completion_chunk(model, {"content": visible_text}, None, completion_id, created)
    for index, call in enumerate(tool_calls):
        yield completion_chunk(
            model,
            {"tool_calls": [{"index": index, **call}]},
            None,
            completion_id,
            created,
        )
    yield completion_chunk(model, {}, "tool_calls" if tool_calls else "stop", completion_id, created)


def image_result_content(result: dict[str, Any]) -> str:
    data = result.get("data")
    if isinstance(data, list) and data:
        return build_chat_image_markdown_content(result)
    return str(result.get("message") or "Image generation completed.")


def image_chat_response(body: dict[str, Any]) -> dict[str, Any]:
    model, prompt, n, images = chat_image_args(body)
    result = collect_image_outputs(stream_image_outputs_with_pool(ConversationRequest(
        prompt=prompt,
        model=model,
        n=n,
        response_format="b64_json",
        images=encode_images(images) or None,
    )))
    response = completion_response(model, image_result_content(result), int(result.get("created") or 0) or None)
    usage = image_usage(
        input_text_tokens=count_text_tokens(prompt, model),
        input_image_tokens=count_image_inputs_tokens(images, model),
        output_tokens=count_image_output_items_tokens(result.get("data")),
    )
    response["usage"] = chat_usage_from_image_usage(usage)
    return response


def image_chat_events(body: dict[str, Any]) -> Iterator[dict[str, Any]]:
    model, prompt, n, images = chat_image_args(body)
    image_outputs = stream_image_outputs_with_pool(ConversationRequest(
        prompt=prompt,
        model=model,
        n=n,
        response_format="b64_json",
        images=encode_images(images) or None,
    ))
    yield from stream_image_chat_completion(image_outputs, model)


def stream_image_chat_completion(image_outputs: Iterable[ImageOutput], model: str) -> Iterator[dict[str, Any]]:
    completion_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())
    sent_role = False
    sent_text = ""
    for output in image_outputs:
        content = ""
        if output.kind == "progress":
            content = output.text
            sent_text += content
        elif output.kind == "result":
            content = build_chat_image_markdown_content({"data": output.data})
        elif output.kind == "message":
            content = output.text[len(sent_text):] if output.text.startswith(sent_text) else output.text
        if not content:
            continue
        if not sent_role:
            sent_role = True
            yield completion_chunk(model, {"role": "assistant", "content": content}, None, completion_id, created)
        else:
            yield completion_chunk(model, {"content": content}, None, completion_id, created)
    if not sent_role:
        yield completion_chunk(model, {"role": "assistant", "content": ""}, None, completion_id, created)
    yield completion_chunk(model, {}, "stop", completion_id, created)


def handle(body: dict[str, Any]) -> dict[str, Any] | Iterator[dict[str, Any]]:
    if body.get("stream"):
        if is_image_chat_request(body):
            return image_chat_events(body)
        model, messages = text_chat_parts(body)
        if is_web_search_chat_request(body) and has_function_tools(body):
            raise HTTPException(
                status_code=400,
                detail={"error": "mixing hosted web_search and client function tools is not supported"},
            )
        if is_web_search_chat_request(body) and not has_unsupported_tools(body, SUPPORTED_CHAT_TOOL_TYPES):
            return stream_web_search_chat_completion(body, messages, model)
        thinking_effort = thinking_effort_from_body(body)
        key = cache_key(body, messages, stream=True)
        if has_function_tools(body):
            return chat_completion_cache.get_or_compute_stream(
                key,
                lambda: stream_function_tool_chat_completion(body, messages, model, thinking_effort),
            )
        return chat_completion_cache.get_or_compute_stream(
            key,
            lambda: stream_text_chat_completion(text_backend(model), messages, model, thinking_effort),
        )
    if is_image_chat_request(body):
        return image_chat_response(body)
    model, messages = text_chat_parts(body)
    if is_web_search_chat_request(body) and has_function_tools(body):
        raise HTTPException(
            status_code=400,
            detail={"error": "mixing hosted web_search and client function tools is not supported"},
        )
    if is_web_search_chat_request(body) and not has_unsupported_tools(body, SUPPORTED_CHAT_TOOL_TYPES):
        return web_search_chat_response(body, messages, model)
    thinking_effort = thinking_effort_from_body(body)
    key = cache_key(body, messages, stream=False)
    if has_function_tools(body):
        return chat_completion_cache.get_or_compute_response(
            key,
            lambda: function_tool_chat_response(body, messages, model, thinking_effort),
        )
    return chat_completion_cache.get_or_compute_response(
        key,
        lambda: text_chat_response(messages, model, thinking_effort),
    )


def text_chat_response(
    messages: list[dict[str, Any]],
    model: str,
    thinking_effort: str = "",
) -> dict[str, Any]:
    output = collect_text_output(
        text_backend(model),
        ConversationRequest(model=model, messages=messages, thinking_effort=thinking_effort),
    )
    return completion_response(
        model,
        output.content,
        messages=messages,
        reasoning_content=output.reasoning_content,
    )
