from __future__ import annotations

import json
import unittest
from unittest import mock

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import api.ai as ai
from api.errors import install_exception_handlers
from services.config import config
from services.log_service import LoggedCall
from services.model_service import ModelUnavailableError
from services.openai_backend_api import OpenAIBackendAPI
from services.protocol import conversation, openai_v1_chat_complete as chat, openai_v1_response as responses, web_search_tool
from services.protocol.chat_completion_cache import chat_completion_cache


BODY = {"model": "gpt-6.1-sol", "messages": [{"role": "user", "content": "hello"}]}


class ChatRequestValidationTests(unittest.TestCase):
    def test_unsupported_controls_fail_before_backend_or_cache(self):
        cases = {
            "n": 2, "max_completion_tokens": 200, "max_tokens": 200,
            "temperature": 0.5, "top_p": 0.9, "seed": 42, "stop": ["END"],
            "response_format": {"type": "json_schema", "json_schema": {}},
            "logprobs": True, "store": True, "service_tier": "ultrafast",
            "modalities": ["audio"], "functions": [],
        }
        for stream in (False, True):
            for param, value in cases.items():
                with self.subTest(stream=stream, param=param), mock.patch.object(chat, "text_backend") as backend:
                    with self.assertRaises(HTTPException) as caught:
                        chat.handle({**BODY, "stream": stream, param: value})
                    self.assertEqual(caught.exception.status_code, 400)
                    self.assertEqual(caught.exception.detail["error"]["param"], param)
                    backend.assert_not_called()

    def test_invalid_messages_are_not_dropped_or_coerced(self):
        for messages in (
            [], [None], [{"role": "admin", "content": "hello"}],
            [{"role": "user", "content": {"text": "hello"}}],
            [{"role": "user", "content": [{"type": "input_audio", "input_audio": {}}]}],
            [{"role": "tool", "content": "result"}],
        ):
            with self.subTest(messages=messages), self.assertRaises(HTTPException):
                chat.text_chat_parts({**BODY, "messages": messages})

    def test_unsupported_or_malformed_tools_are_rejected(self):
        for tools in (
            [{"type": "computer_use"}], [None], {"type": "function"},
            [{"type": "function", "function": {"name": "lookup", "strict": True}}],
        ):
            with self.subTest(tools=tools), self.assertRaises(HTTPException):
                chat.text_chat_parts({**BODY, "tools": tools})

    def test_malformed_tool_choice_is_rejected_without_declared_tools(self):
        for choice in ({}, {"type": "function", "function": {}}, 7):
            with self.subTest(choice=choice), self.assertRaises(HTTPException):
                chat.text_chat_parts({**BODY, "tool_choice": choice})

    def test_developer_messages_use_web_system_role(self):
        backend = OpenAIBackendAPI("test")
        self.addCleanup(backend.close)
        messages = [{"role": "developer", "content": "Return Chinese."}, *BODY["messages"]]
        output = backend._api_messages_to_conversation_messages(messages)
        self.assertEqual(output[0]["author"]["role"], "system")
        self.assertEqual(output[0]["content"]["parts"], ["Return Chinese."])
        self.assertEqual(messages[0]["role"], "developer")

    def test_reasoning_none_bypasses_configured_default(self):
        backend = OpenAIBackendAPI("test")
        self.addCleanup(backend.close)
        with mock.patch.dict(config.data, {"default_thinking_effort": "extended"}):
            for adapter, field in ((chat, {"reasoning_effort": "none"}), (responses, {"reasoning": {"effort": "none"}})):
                effort = adapter.thinking_effort_from_body(field)
                payload = backend._conversation_payload(BODY["messages"], BODY["model"], "Asia/Shanghai", effort)
                self.assertNotIn("thinking_effort", payload)
            default = backend._conversation_payload(BODY["messages"], BODY["model"], "Asia/Shanghai")
            self.assertEqual(default["thinking_effort"], "extended")

    def test_responses_null_effort_falls_back_and_supports_full_bridge_scale(self):
        self.assertEqual(responses.thinking_effort_from_body({"reasoning": {"effort": None}, "reasoning_effort": "max"}), "max")
        self.assertEqual(responses.thinking_effort_from_body({"thinking_effort": None, "reasoning_effort": "minimal"}), "minimal")

    def test_invalid_reasoning_is_rejected_before_stream_creation(self):
        for adapter, body in ((chat, BODY), (responses, {"input": "hello"})):
            with self.subTest(adapter=adapter.__name__), self.assertRaises(HTTPException):
                adapter.handle({**body, "stream": True, "reasoning": {"effort": "typo"}})

    def test_chat_model_effort_limits_fail_before_backend_or_cache(self):
        for adapter, body, fields, param in (
            (chat, BODY, {"reasoning_effort": "high"}, "reasoning_effort"),
            (responses, {"input": "hello"}, {"reasoning": {"effort": "high"}}, "reasoning.effort"),
        ):
            for model in ("gpt-6-pro", "gpt-5.6-instant", "gpt-5-6-instant"):
                for stream in (False, True):
                    with self.subTest(model=model, stream=stream, adapter=adapter.__name__), mock.patch.object(adapter, "text_backend") as backend:
                        with self.assertRaises(HTTPException) as caught:
                            adapter.handle({**body, **fields, "model": model, "stream": stream})
                        self.assertEqual(caught.exception.status_code, 400)
                        self.assertEqual(caught.exception.detail["error"]["param"], param)
                        backend.assert_not_called()

    def test_chat_pro_accepts_standard_effort_and_native_model(self):
        for effort in (None, "medium", "standard", "none", "auto"):
            with self.subTest(effort=effort):
                model, _messages = chat.text_chat_parts({**BODY, "model": "gpt-6-pro", "reasoning_effort": effort})
                self.assertEqual(model, "gpt-6-pro")

    def test_chat_defaults_do_not_inherit_work_thinking_effort(self):
        backend = OpenAIBackendAPI("test")
        self.addCleanup(backend.close)
        with mock.patch.dict(config.data, {"default_thinking_effort": "max"}):
            pro = backend._conversation_payload(BODY["messages"], "gpt-6-pro", "Asia/Shanghai")
            instant = backend._conversation_payload(BODY["messages"], "gpt-5-6-instant", "Asia/Shanghai")
            thinking = backend._conversation_payload(BODY["messages"], "gpt-5-6-thinking", "Asia/Shanghai")
        self.assertEqual(pro["model"], "gpt-6-pro")
        self.assertEqual(pro["thinking_effort"], "standard")
        self.assertNotIn("thinking_effort", instant)
        self.assertEqual(thinking["thinking_effort"], "max")

    def test_search_preserves_requested_model_and_resolves_web_alias(self):
        backend = mock.Mock()
        backend.search.return_value = {"answer": "ok"}
        with (
            mock.patch.object(web_search_tool.account_service, "get_text_access_token", return_value="test") as select,
            mock.patch.object(web_search_tool.account_service, "mark_text_used"),
            mock.patch.object(web_search_tool.model_catalog_service, "resolve_model", return_value="gpt-6-1-sol-wm") as resolve,
            mock.patch.object(web_search_tool, "OpenAIBackendAPI", return_value=backend),
        ):
            web_search_tool.run_web_search("news", model="gpt-6.1-sol")
        select.assert_called_once_with(model="gpt-6.1-sol")
        resolve.assert_called_once_with("gpt-6.1-sol")
        backend.search.assert_called_once_with("news", model="gpt-6-1-sol-wm")
        backend.close.assert_called_once()

    def test_initial_text_backend_is_reused_and_closed(self):
        backend = mock.Mock(access_token="test")
        with (
            mock.patch.object(conversation, "OpenAIBackendAPI") as create,
            mock.patch.object(conversation, "conversation_events", return_value=iter([{"type": "conversation.delta", "delta": "ok"}])),
            mock.patch.object(conversation.account_service, "mark_text_used"),
        ):
            output = list(conversation.stream_text_parts(backend, conversation.ConversationRequest(model="gpt-6.1-sol")))
        self.assertEqual(output, [("content", "ok")])
        create.assert_not_called()
        backend.close.assert_called_once()


class ChatHTTPContractTests(unittest.TestCase):
    def setUp(self):
        chat_completion_cache.clear()
        self.addCleanup(chat_completion_cache.clear)
        app = FastAPI()
        install_exception_handlers(app)
        app.include_router(ai.create_router())
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        for patcher in (
            mock.patch.object(ai, "require_identity", return_value={"id": "test"}),
            mock.patch.object(ai, "check_request"),
            mock.patch.object(LoggedCall, "log"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_unsupported_parameter_is_openai_400_json_even_for_stream(self):
        response = self.client.post("/v1/chat/completions", json={**BODY, "stream": True, "max_completion_tokens": 20})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["param"], "max_completion_tokens")
        self.assertEqual(response.json()["error"]["type"], "invalid_request_error")

    def test_unknown_model_is_404_for_stream_and_nonstream(self):
        for stream in (False, True):
            with self.subTest(stream=stream), mock.patch.object(chat, "text_backend", side_effect=ModelUnavailableError("model unavailable")):
                response = self.client.post("/v1/chat/completions", json={**BODY, "stream": stream})
            self.assertEqual(response.status_code, 404)
            self.assertEqual(response.json()["error"]["code"], "model_not_found")
            self.assertEqual(response.json()["error"]["param"], "model")

    def test_retired_models_cannot_reach_generation_cache_or_search(self):
        for model in ("auto", "gpt-4o", "gpt-5.5", "gpt-5.6-luna", "chat-latest", "gpt-5-search-api"):
            for stream in (False, True):
                for endpoint, payload in (
                    ("/v1/chat/completions", {"messages": BODY["messages"]}),
                    ("/v1/chat/completions", {"messages": BODY["messages"], "tools": [{"type": "web_search"}]}),
                    ("/v1/responses", {"input": "hello"}),
                    ("/v1/messages", {"messages": BODY["messages"]}),
                ):
                    with (
                        self.subTest(model=model, stream=stream, endpoint=endpoint),
                        mock.patch.object(chat_completion_cache, "get_or_compute_response") as cache,
                        mock.patch.object(chat_completion_cache, "get_or_compute_stream") as stream_cache,
                        mock.patch.object(chat, "run_web_search") as search,
                    ):
                        response = self.client.post(endpoint, json={**payload, "model": model, "stream": stream})
                        self.assertEqual(response.status_code, 404)
                        cache.assert_not_called()
                        stream_cache.assert_not_called()
                        search.assert_not_called()

    def test_omitted_chat_model_defaults_to_latest_retained_model(self):
        with (
            mock.patch.object(chat, "text_backend", return_value=object()) as backend,
            mock.patch.object(chat, "collect_text_output", return_value=conversation.TextCompletionOutput(content="ok")),
        ):
            response = self.client.post("/v1/chat/completions", json={"messages": BODY["messages"]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["model"], "gpt-6.1-sol")
        backend.assert_called_once_with("gpt-6.1-sol")

    def test_sse_usage_has_one_terminal_chunk_and_done(self):
        with (
            mock.patch.object(chat, "text_backend", return_value=object()),
            mock.patch.object(chat, "stream_text_parts", return_value=iter([("content", "answer")])),
        ):
            response = self.client.post("/v1/chat/completions", json={**BODY, "stream": True, "stream_options": {"include_usage": True}})
        data = [line[6:] for line in response.text.splitlines() if line.startswith("data: ")]
        self.assertEqual(data[-1], "[DONE]")
        chunks = [json.loads(item) for item in data[:-1]]
        self.assertEqual(chunks[-1]["choices"], [])
        self.assertEqual(chunks[-2]["choices"][0]["finish_reason"], "stop")
        self.assertEqual(len({chunk["id"] for chunk in chunks}), 1)
        self.assertTrue(all(chunk["usage"] is None for chunk in chunks[:-1]))

    def test_interrupted_stream_returns_standard_error_without_success(self):
        def broken(_backend, _request):
            yield "content", "partial"
            raise RuntimeError("upstream unavailable")

        with (
            mock.patch.object(chat, "text_backend", return_value=object()),
            mock.patch.object(chat, "stream_text_parts", side_effect=broken),
        ):
            response = self.client.post("/v1/chat/completions", json={**BODY, "stream": True, "stream_options": {"include_usage": True}})
        payloads = [line[6:] for line in response.text.splitlines() if line.startswith("data: ")]
        self.assertEqual(payloads[-1], "[DONE]")
        chunks = [json.loads(value) for value in payloads[:-1]]
        self.assertEqual(chunks[-1]["error"]["type"], "server_error")
        self.assertEqual(chunks[-1]["error"]["code"], "upstream_error")
        self.assertIn("param", chunks[-1]["error"])
        self.assertFalse(any(chunk.get("usage") for chunk in chunks))
        self.assertFalse(any(choice.get("finish_reason") for chunk in chunks for choice in chunk.get("choices", [])))


if __name__ == "__main__":
    unittest.main()
