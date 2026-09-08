from __future__ import annotations

import html
import json
import re
from typing import Any


FUNCTION_TOOL_TYPE = "function"
_FUNCTION_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_TOOL_MARKUP_RE = re.compile(
    r"(?is)<tool_calls\b[^>]*>.*?</tool_calls>|"
    r"<tool_call\b[^>]*>.*?</tool_call>|"
    r"<function_call\b[^>]*>.*?</function_call>|"
    r"<invoke\b[^>]*>.*?</invoke>"
)


def function_tools(body: dict[str, Any]) -> list[dict[str, Any]]:
    tools = body.get("tools")
    if not isinstance(tools, list):
        return []
    return [
        tool
        for tool in tools
        if isinstance(tool, dict)
        and str(tool.get("type") or "").strip() == FUNCTION_TOOL_TYPE
        and isinstance(tool.get("function"), dict)
        and _FUNCTION_NAME_RE.fullmatch(str(tool["function"].get("name") or "").strip())
    ]


def has_function_tools(body: dict[str, Any]) -> bool:
    return bool(function_tools(body)) and body.get("tool_choice") != "none"


def _function_name_from_choice(tool_choice: object) -> str:
    if not isinstance(tool_choice, dict) or tool_choice.get("type") != FUNCTION_TOOL_TYPE:
        return ""
    function = tool_choice.get("function")
    return str(function.get("name") or "").strip() if isinstance(function, dict) else ""


def _tool_choice_instruction(body: dict[str, Any], names: set[str]) -> str:
    choice = body.get("tool_choice", "auto")
    forced_name = _function_name_from_choice(choice)
    if forced_name:
        if forced_name not in names:
            raise ValueError(f"tool_choice references unknown function {forced_name!r}")
        return f"You MUST call the function {forced_name!r}."
    if choice == "required":
        return "You MUST call at least one available function."
    return "Call a function only when it is needed; otherwise answer normally."


def build_function_tool_prompt(body: dict[str, Any]) -> str:
    declared_tools = [
        tool
        for tool in body.get("tools", [])
        if isinstance(tool, dict) and str(tool.get("type") or "").strip() == FUNCTION_TOOL_TYPE
    ] if isinstance(body.get("tools"), list) else []
    tools = function_tools(body)
    if len(tools) != len(declared_tools):
        raise ValueError("each function tool requires function.name")
    choice = body.get("tool_choice", "auto")
    if isinstance(choice, str) and choice not in {"auto", "none", "required"}:
        raise ValueError(f"unsupported tool_choice {choice!r}")
    forced_name = _function_name_from_choice(choice)
    if not tools and (choice == "required" or forced_name):
        raise ValueError("tool_choice requires at least one function tool")
    if not tools or choice == "none":
        return ""
    specs = []
    names: set[str] = set()
    for tool in tools:
        function = tool["function"]
        name = str(function.get("name") or "").strip()
        names.add(name)
        specs.append({
            "name": name,
            "description": str(function.get("description") or ""),
            "parameters": function.get("parameters") or {"type": "object", "properties": {}},
            "strict": bool(function.get("strict", False)),
        })
    choice_instruction = _tool_choice_instruction(body, names)
    parallel_instruction = (
        "You may return multiple tool_call blocks when useful."
        if body.get("parallel_tool_calls", True) is not False
        else "Return at most one tool_call block."
    )
    return (
        "You are connected to an OpenAI Chat Completions client that can execute the functions below.\n"
        f"Available functions: {json.dumps(specs, ensure_ascii=False, separators=(',', ':'))}\n\n"
        "Function calling rules:\n"
        f"- {choice_instruction}\n"
        f"- {parallel_instruction}\n"
        "- Never execute a client function yourself and never invent its result.\n"
        "- A <tool_result> message means the client already executed that function. Use that result to answer the "
        "original request; do not repeat the same call unless the result explicitly reports an error or missing data.\n"
        "- When calling functions, output only this XML envelope and no markdown:\n"
        "<tool_calls><tool_call><tool_name>FUNCTION_NAME</tool_name>"
        "<arguments><![CDATA[{\"argument\":\"value\"}]]></arguments>"
        "</tool_call></tool_calls>\n"
        "- Arguments must be one valid JSON object matching the supplied parameters schema."
    )


def _message_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict) and item.get("type") in {"text", "input_text", "output_text"}:
            parts.append(str(item.get("text") or ""))
    return "".join(parts)


def _history_tool_call_markup(tool_calls: object) -> str:
    if not isinstance(tool_calls, list):
        return ""
    blocks: list[str] = []
    for item in tool_calls:
        if not isinstance(item, dict):
            continue
        function = item.get("function")
        if not isinstance(function, dict):
            continue
        name = str(function.get("name") or "").strip()
        arguments = function.get("arguments")
        if not name:
            continue
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments or {}, ensure_ascii=False, separators=(",", ":"))
        blocks.append(
            "<tool_call>"
            f"<tool_name>{html.escape(name)}</tool_name>"
            f"<arguments><![CDATA[{arguments.replace(']]>', ']]\\u003e')}]]></arguments>"
            "</tool_call>"
        )
    return f"<tool_calls>{''.join(blocks)}</tool_calls>" if blocks else ""


def preprocess_function_tool_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Translate OpenAI tool history into text that ChatGPT Web can consume safely."""
    output: list[dict[str, Any]] = []
    for message in messages:
        role = str(message.get("role") or "user").strip().lower()
        if role == "assistant" and message.get("tool_calls"):
            content = _message_text(message.get("content"))
            markup = _history_tool_call_markup(message.get("tool_calls"))
            output.append({
                "role": "assistant",
                "content": "\n\n".join(part for part in (content, markup) if part),
            })
            continue
        if role == "tool":
            call_id = str(message.get("tool_call_id") or "").strip()
            name = str(message.get("name") or "").strip()
            label = name or call_id or "unknown"
            result_text = _message_text(message.get("content")).replace("]]>", "]]\\u003e")
            output.append({
                "role": "user",
                "content": (
                    f"The client executed function {label!r}. Its authoritative result follows. "
                    "Continue the original conversation using this result.\n"
                    f'<tool_result name="{html.escape(label, quote=True)}"><![CDATA[{result_text}]]></tool_result>'
                ),
            })
            continue
        output.append(dict(message))
    return output


def _xml_value(text: str, tag: str) -> str:
    match = re.search(rf"(?is)<{tag}\b[^>]*>(.*?)</{tag}>", text)
    if not match:
        return ""
    value = match.group(1).strip()
    cdata = re.fullmatch(r"(?is)<!\[CDATA\[(.*?)]]>", value)
    return html.unescape(cdata.group(1) if cdata else value).strip()


def _canonical_arguments(value: object) -> str | None:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return None
    if not isinstance(value, dict):
        return None
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _json_tool_calls(text: str) -> list[tuple[str, str]]:
    candidate = re.sub(r"(?is)^\s*```(?:json)?\s*|\s*```\s*$", "", text or "").strip()
    try:
        value = json.loads(candidate)
    except json.JSONDecodeError:
        return []
    items = value.get("tool_calls") if isinstance(value, dict) else value
    if not isinstance(items, list):
        return []
    calls: list[tuple[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        function = item.get("function") if isinstance(item.get("function"), dict) else item
        name = str(function.get("name") or "").strip()
        arguments = _canonical_arguments(function.get("arguments", function.get("parameters", {})))
        if name and arguments is not None:
            calls.append((name, arguments))
    return calls


def parsed_function_calls(text: str, tools: list[dict[str, Any]]) -> list[tuple[str, str]]:
    allowed_names = {
        str(tool["function"].get("name") or "").strip()
        for tool in tools
        if isinstance(tool.get("function"), dict)
    }
    calls: list[tuple[str, str]] = []
    blocks = re.findall(
        r"(?is)<tool_call\b[^>]*>(.*?)</tool_call>|"
        r"<function_call\b[^>]*>(.*?)</function_call>|"
        r"<invoke\b[^>]*>(.*?)</invoke>",
        text or "",
    )
    for match in blocks:
        block = next((part for part in match if part), "")
        name = _xml_value(block, "tool_name") or _xml_value(block, "name") or _xml_value(block, "function")
        raw_arguments = (
            _xml_value(block, "arguments")
            or _xml_value(block, "parameters")
            or _xml_value(block, "input")
            or "{}"
        )
        arguments = _canonical_arguments(raw_arguments)
        if name in allowed_names and arguments is not None:
            calls.append((name, arguments))
    if not calls:
        calls = [call for call in _json_tool_calls(text) if call[0] in allowed_names]
    return calls


def strip_function_tool_markup(text: str) -> str:
    if _json_tool_calls(text):
        return ""
    return _TOOL_MARKUP_RE.sub("", text or "").strip()


def has_function_call_syntax(text: str) -> bool:
    return bool(_TOOL_MARKUP_RE.search(text or "") or _json_tool_calls(text))
