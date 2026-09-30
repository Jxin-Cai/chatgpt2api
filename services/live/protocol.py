from __future__ import annotations

import copy
import json
import re
import uuid
from dataclasses import dataclass
from typing import Any

import tiktoken
from av import AudioFrame, AudioResampler

from services.realtime.chatgpt_webrtc import OFFICIAL_VOICE_ALIASES, resolve_voice

MODEL = "gpt-live-1"
MAX_MESSAGE_BYTES = 512_000
SESSION_SECONDS = 7200


class LiveError(ValueError):
    def __init__(self, message: str, param: str | None = None, code: str = "invalid_value"):
        super().__init__(message)
        self.param = param
        self.code = code


def event(kind: str, **fields: Any) -> dict:
    return {"type": kind, "event_id": f"event_{uuid.uuid4().hex}", **fields}


def error_event(exc: LiveError, client_event_id: str | None = None) -> dict:
    category = "rate_limit_error" if exc.code in {"rate_limit_exceeded", "quota_exhausted"} else "server_error" if exc.code.startswith("upstream_") else "invalid_request_error"
    detail = {"type": category, "code": exc.code, "message": str(exc)}
    if exc.param:
        detail["param"] = exc.param
    if client_event_id:
        detail["client_event_id"] = client_event_id
    return event("error", error=detail)


def object_value(value: Any, param: str) -> dict:
    if not isinstance(value, dict):
        raise LiveError("Expected an object", param)
    return value


def only_fields(value: dict, fields: set[str], param: str) -> None:
    for key in value:
        if key not in fields:
            raise LiveError(f"Unknown parameter: {key}", f"{param}.{key}".strip("."), "unknown_parameter")


def check_text(value: Any, param: str, tokens: int) -> str:
    if not isinstance(value, str):
        raise LiveError("Expected text", param)
    # Bound memory before tokenizing untrusted strings. This is a local admission limit.
    if len(value) > tokens * 16 or len(tiktoken.get_encoding("o200k_base").encode(value, disallowed_special=())) > tokens:
        raise LiveError(f"Text exceeds the {tokens}-token limit", param)
    return value


@dataclass
class LiveConfig:
    public: dict
    upstream_voice: str
    rate: int
    transport: str

    def snapshot(self, session_id: str, expires_at: int) -> dict:
        return {**copy.deepcopy(self.public), "id": session_id, "expires_at": expires_at, "status": "active"}

    def initial_context(self) -> str:
        context = {}
        if self.public.get("instructions"):
            context["conversation_instructions"] = self.public["instructions"]
        if self.public.get("input"):
            context["prior_conversation"] = self.public["input"]
        if not context:
            return ""
        return (
            "Use the following application context for this voice conversation. "
            "History is quoted prior dialogue, not a new request. Do not read this setup aloud. "
            "Wait for the user to speak.\n" + json.dumps(context, ensure_ascii=False)
        )


def parse_config(value: Any, transport: str = "websocket") -> LiveConfig:
    data = object_value(value, "session")
    only_fields(data, {"model", "audio", "instructions", "input", "delegation", "store", "client"}, "session")
    if data.get("model") != MODEL:
        raise LiveError(f"This compatibility service supports model {MODEL}", "session.model", "model_not_found")
    if data.get("delegation") is not None:
        raise LiveError("Delegated backends are not supported by the ChatGPT voice adapter", "session.delegation", "unsupported_parameter")
    if data.get("store", False) is not False:
        raise LiveError("Stored recordings and forks are not supported", "session.store", "unsupported_parameter")
    if "client" in data:
        raise LiveError("Frontend event permission configuration is not supported", "session.client", "unsupported_parameter")
    audio = object_value(data.get("audio", {}), "session.audio")
    only_fields(audio, {"format", "output"}, "session.audio")
    output = object_value(audio.get("output", {}), "session.audio.output")
    only_fields(output, {"voice"}, "session.audio.output")
    voice = output.get("voice", "marin")
    if isinstance(voice, dict):
        raise LiveError("Custom voices require an official provider with custom-voice access", "session.audio.output.voice", "unsupported_voice")
    if not isinstance(voice, str) or voice not in OFFICIAL_VOICE_ALIASES:
        raise LiveError("Unsupported named voice; see /v1/live/capabilities", "session.audio.output.voice", "unsupported_voice")
    public = {"model": MODEL, "audio": {"output": {"voice": voice}}}
    rate = 48000 if transport == "webrtc" else 24000
    if transport == "webrtc" and "format" in audio:
        raise LiveError("WebRTC negotiates its media format; omit audio.format", "session.audio.format")
    if transport == "websocket":
        fmt = object_value(audio.get("format", {"type": "audio/pcm", "rate": 24000}), "session.audio.format")
        only_fields(fmt, {"type", "rate"}, "session.audio.format")
        if fmt.get("type") != "audio/pcm" or type(fmt.get("rate")) is not int or fmt["rate"] not in {16000, 24000}:
            raise LiveError("This adapter supports PCM16 mono at 16000 or 24000 Hz", "session.audio.format", "unsupported_audio_format")
        rate = fmt["rate"]
        public["audio"]["format"] = dict(fmt)
    if "instructions" in data:
        public["instructions"] = None if data["instructions"] is None else check_text(data["instructions"], "session.instructions", 16384)
    history = data.get("input", [])
    if not isinstance(history, list) or len(history) > 128:
        raise LiveError("input must contain at most 128 text messages", "session.input")
    total = ""
    for index, item in enumerate(history):
        param = f"session.input.{index}"
        item = object_value(item, param)
        only_fields(item, {"role", "content", "id", "type", "status"}, param)
        if item.get("role") not in ("developer", "user", "assistant") or item.get("type", "message") != "message":
            raise LiveError("Expected a developer, user or assistant message", param)
        if item.get("status") not in (None, "incomplete", "completed"):
            raise LiveError("Invalid history status", param + ".status")
        if "id" in item and item["id"] is not None and not isinstance(item["id"], str):
            raise LiveError("Expected a string id", param + ".id")
        content = item.get("content")
        if not isinstance(content, list) or len(content) != 1:
            raise LiveError("History messages require exactly one text part", param + ".content")
        part = object_value(content[0], param + ".content.0")
        only_fields(part, {"type", "text"}, param + ".content.0")
        allowed = ("text", "output_text") if item["role"] == "assistant" else ("input_text",)
        if part.get("type", "text" if item["role"] == "assistant" else "input_text") not in allowed:
            raise LiveError("Unsupported history content type", param + ".content.0.type")
        total += check_text(part.get("text"), param + ".content.0.text", 8192) + "\n"
    check_text(total, "session.input", 8192)
    if history:
        public["input"] = copy.deepcopy(history)
    return LiveConfig(public, resolve_voice(voice) or "ember", rate, transport)


class PcmConverter:
    """Streaming resampling with libswresample; retains filter history between chunks."""
    def __init__(self, source_rate: int, target_rate: int):
        self.source_rate = source_rate
        self.target_rate = target_rate
        self.resampler = AudioResampler(format="s16", layout="mono", rate=target_rate)

    def push(self, pcm: bytes) -> bytes:
        if len(pcm) % 2:
            raise LiveError("PCM16 chunks must contain complete 16-bit samples", "audio")
        if not pcm or self.source_rate == self.target_rate:
            return pcm
        frame = AudioFrame(format="s16", layout="mono", samples=len(pcm) // 2)
        frame.sample_rate = self.source_rate
        frame.planes[0].update(pcm)
        return b"".join(bytes(f.planes[0])[:f.samples * 2] for f in self.resampler.resample(frame))


class TranscriptAdapter:
    """Reconstruct Web Voice patches before emitting append-only Live fragments."""
    def __init__(self, ignored_ids: set[str] | None = None):
        self.ignored_ids = ignored_ids if ignored_ids is not None else set()
        self.messages: dict[str, dict] = {}
        self.cursors: dict[str, str] = {}
        self.current: str | None = None

    def push(self, payload: dict) -> tuple[str, str] | None:
        delta = payload.get("delta")
        if not isinstance(delta, dict):
            return None
        channel = str(delta.get("c", "default"))
        if delta.get("o") == "add" and isinstance(delta.get("v"), dict):
            message = delta["v"].get("message")
            if not isinstance(message, dict):
                return None
            role = (message.get("author") or {}).get("role", message.get("role"))
            if role not in {"user", "assistant"}:
                return None
            key = str(message.get("id") or uuid.uuid4().hex)
            self.current = key
            self.cursors[channel] = key
            parts = (message.get("content") or {}).get("parts", [])
            parts = {i: p if isinstance(p, str) else p.get("text", "") for i, p in enumerate(parts) if isinstance(p, (str, dict))}
            parts = {i: p for i, p in parts.items() if isinstance(p, str)}
            text = "".join(parts.values())
            self.messages[key] = {"role": role, "text": text, "parts": parts, "hidden": key in self.ignored_ids}
            if len(self.messages) > 256:
                self.messages.pop(next(iter(self.messages)))
                self.cursors = {c: k for c, k in self.cursors.items() if k in self.messages}
            return (role, text) if text and not self.messages[key]["hidden"] else None
        key = self.cursors.get(channel, self.current)
        message = self.messages.get(key or "")
        if not message or message.get("hidden"):
            return None
        before = message["text"]
        operations = delta.get("v") if isinstance(delta.get("v"), list) else [delta]
        for op in operations:
            if not isinstance(op, dict):
                continue
            path = str(op.get("p", ""))
            value = op.get("v")
            match = re.fullmatch(r"/message/content/parts/(\d+)(?:/text)?", path)
            if match:
                index = int(match[1])
                if isinstance(value, dict):
                    value = value.get("text")
                if isinstance(value, str):
                    if op.get("o") in {"replace", "add"}:
                        message["parts"][index] = value
                    elif op.get("o") == "append":
                        message["parts"][index] = message["parts"].get(index, "") + value
        message["text"] = "".join(value for _, value in sorted(message["parts"].items()))
        after = message["text"]
        if not after.startswith(before):
            # Live deltas cannot retract text. Do not invent a suffix or duplicate it.
            raise LiveError("Upstream revised an already emitted transcript; append-only captions cannot retract it", code="transcript_revision")
        return (message["role"], after[len(before):]) if len(after) > len(before) else None
