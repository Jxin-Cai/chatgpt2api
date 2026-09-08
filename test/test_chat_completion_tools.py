from __future__ import annotations

import unittest
from unittest import mock

from fastapi import HTTPException

from services.openai_backend_api import OpenAIBackendAPI
from services.protocol import openai_v1_chat_complete
from services.protocol.function_tool_bridge import preprocess_function_tool_messages
from services.protocol import web_search_tool
from services.protocol.conversation import TextCompletionOutput, iter_conversation_payloads
from services.protocol.web_search_tool import search_prompt_from_messages


WEATHER_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get current weather",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    },
}


class ChatCompletionFunctionToolTests(unittest.TestCase):
    def test_non_stream_returns_standard_tool_calls(self) -> None:
        body = {
            "model": "auto",
            "messages": [{"role": "user", "content": "上海天气如何？"}],
            "tools": [WEATHER_TOOL],
            "tool_choice": "required",
        }
        raw = (
            "<tool_calls><tool_call><tool_name>get_weather</tool_name>"
            "<arguments><![CDATA[{\"city\":\"上海\"}]]></arguments>"
            "</tool_call></tool_calls>"
        )
        with (
            mock.patch.object(openai_v1_chat_complete, "text_backend", return_value=object()),
            mock.patch.object(
                openai_v1_chat_complete,
                "collect_text_output",
                return_value=TextCompletionOutput(content=raw),
            ) as collect,
        ):
            response = openai_v1_chat_complete.handle(body)

        choice = response["choices"][0]
        self.assertEqual(choice["finish_reason"], "tool_calls")
        self.assertIsNone(choice["message"]["content"])
        call = choice["message"]["tool_calls"][0]
        self.assertTrue(call["id"].startswith("call_"))
        self.assertEqual(call["type"], "function")
        self.assertEqual(call["function"], {"name": "get_weather", "arguments": '{"city":"上海"}'})
        sent_messages = collect.call_args.args[1].messages
        self.assertEqual(sent_messages[0]["role"], "system")
        self.assertIn("You MUST call at least one available function", sent_messages[0]["content"])

    def test_stream_returns_delta_tool_calls_and_tool_finish_reason(self) -> None:
        body = {
            "model": "auto",
            "stream": True,
            "messages": [{"role": "user", "content": "上海天气如何？"}],
            "tools": [WEATHER_TOOL],
        }
        raw = (
            "<tool_calls><tool_call><tool_name>get_weather</tool_name>"
            "<arguments>{\"city\":\"上海\"}</arguments>"
            "</tool_call></tool_calls>"
        )
        with (
            mock.patch.object(openai_v1_chat_complete, "text_backend", return_value=object()),
            mock.patch.object(
                openai_v1_chat_complete,
                "collect_text_output",
                return_value=TextCompletionOutput(content=raw),
            ),
        ):
            chunks = list(openai_v1_chat_complete.handle(body))

        self.assertEqual(chunks[0]["choices"][0]["delta"]["role"], "assistant")
        call_delta = chunks[1]["choices"][0]["delta"]["tool_calls"][0]
        self.assertEqual(call_delta["index"], 0)
        self.assertEqual(call_delta["function"]["name"], "get_weather")
        self.assertEqual(chunks[-1]["choices"][0]["finish_reason"], "tool_calls")

    def test_json_envelope_is_also_accepted_without_leaking_as_content(self) -> None:
        body = {
            "model": "auto",
            "messages": [{"role": "user", "content": "weather"}],
            "tools": [WEATHER_TOOL],
        }
        raw = '{"tool_calls":[{"name":"get_weather","arguments":{"city":"Paris"}}]}'
        with (
            mock.patch.object(openai_v1_chat_complete, "text_backend", return_value=object()),
            mock.patch.object(
                openai_v1_chat_complete,
                "collect_text_output",
                return_value=TextCompletionOutput(content=raw),
            ),
        ):
            response = openai_v1_chat_complete.handle(body)

        self.assertIsNone(response["choices"][0]["message"]["content"])
        self.assertEqual(
            response["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"],
            '{"city":"Paris"}',
        )

    def test_tool_history_is_translated_for_web_follow_up(self) -> None:
        messages = preprocess_function_tool_messages([
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": '{"city":"上海"}'},
                }],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": '{"temperature":25}'},
        ])

        self.assertIn("<tool_calls>", messages[0]["content"])
        self.assertEqual(messages[1]["role"], "user")
        self.assertIn("<tool_result", messages[1]["content"])
        self.assertIn("25", messages[1]["content"])

    def test_unknown_forced_function_is_rejected(self) -> None:
        with self.assertRaises(HTTPException) as raised:
            openai_v1_chat_complete.text_chat_parts({
                "model": "auto",
                "messages": [{"role": "user", "content": "weather"}],
                "tools": [WEATHER_TOOL],
                "tool_choice": {"type": "function", "function": {"name": "missing"}},
            })

        self.assertEqual(raised.exception.status_code, 400)

    def test_required_choice_without_functions_is_rejected(self) -> None:
        with self.assertRaises(HTTPException) as raised:
            openai_v1_chat_complete.text_chat_parts({
                "model": "auto",
                "messages": [{"role": "user", "content": "weather"}],
                "tool_choice": "required",
            })

        self.assertEqual(raised.exception.status_code, 400)

    def test_required_choice_fails_if_model_does_not_call(self) -> None:
        body = {
            "model": "auto",
            "messages": [{"role": "user", "content": "weather"}],
            "tools": [WEATHER_TOOL],
            "tool_choice": "required",
        }
        with (
            mock.patch.object(openai_v1_chat_complete, "text_backend", return_value=object()),
            mock.patch.object(
                openai_v1_chat_complete,
                "collect_text_output",
                return_value=TextCompletionOutput(content="I will answer directly."),
            ),
            self.assertRaisesRegex(RuntimeError, "required function call"),
        ):
            openai_v1_chat_complete.handle(body)

    def test_parallel_tool_calls_false_keeps_one_call(self) -> None:
        body = {
            "model": "auto",
            "messages": [{"role": "user", "content": "weather"}],
            "tools": [WEATHER_TOOL],
            "parallel_tool_calls": False,
        }
        raw = (
            "<tool_calls>"
            "<tool_call><tool_name>get_weather</tool_name><arguments>{\"city\":\"上海\"}</arguments></tool_call>"
            "<tool_call><tool_name>get_weather</tool_name><arguments>{\"city\":\"北京\"}</arguments></tool_call>"
            "</tool_calls>"
        )
        with (
            mock.patch.object(openai_v1_chat_complete, "text_backend", return_value=object()),
            mock.patch.object(
                openai_v1_chat_complete,
                "collect_text_output",
                return_value=TextCompletionOutput(content=raw),
            ),
        ):
            response = openai_v1_chat_complete.handle(body)

        self.assertEqual(len(response["choices"][0]["message"]["tool_calls"]), 1)

    def test_echoed_assistant_history_is_skipped_before_new_answer(self) -> None:
        history = "previous assistant output"
        payloads = iter([
            '{"message":{"author":{"role":"assistant"},"recipient":"all",'
            '"content":{"content_type":"text","parts":["previous assistant output"]}}}',
            '{"message":{"author":{"role":"assistant"},"recipient":"all","channel":"final",'
            '"content":{"content_type":"text","parts":["new answer"]}}}',
            "[DONE]",
        ])

        events = list(iter_conversation_payloads(payloads, history, [history]))

        deltas = [event.get("delta") for event in events if event.get("type") == "conversation.delta"]
        self.assertEqual(deltas, ["new answer"])

    def test_reasoning_recap_is_returned_separately_from_function_call(self) -> None:
        body = {
            "model": "auto",
            "messages": [{"role": "user", "content": "上海天气如何？"}],
            "tools": [WEATHER_TOOL],
        }
        raw = (
            "<tool_calls><tool_call><tool_name>get_weather</tool_name>"
            "<arguments>{\"city\":\"上海\"}</arguments>"
            "</tool_call></tool_calls>"
        )
        with (
            mock.patch.object(openai_v1_chat_complete, "text_backend", return_value=object()),
            mock.patch.object(
                openai_v1_chat_complete,
                "collect_text_output",
                return_value=TextCompletionOutput(content=raw, reasoning_content="准备查询天气"),
            ),
        ):
            response = openai_v1_chat_complete.handle(body)

        message = response["choices"][0]["message"]
        self.assertEqual(message["reasoning_content"], "准备查询天气")
        self.assertIsNone(message["content"])
        self.assertEqual(message["tool_calls"][0]["function"]["name"], "get_weather")


class ChatCompletionReasoningTests(unittest.TestCase):
    def test_recap_is_split_from_final_and_raw_analysis_is_not_exposed(self) -> None:
        payloads = iter([
            '{"message":{"author":{"role":"assistant"},"recipient":"all",'
            '"content":{"content_type":"thoughts","parts":["private chain of thought"]}}}',
            '{"message":{"author":{"role":"assistant"},"recipient":"all",'
            '"content":{"content_type":"reasoning_recap","content":"思考了两秒"}}}',
            '{"message":{"author":{"role":"assistant"},"recipient":"all","channel":"final",'
            '"content":{"content_type":"text","parts":["最终答案"]}}}',
            "[DONE]",
        ])

        events = list(iter_conversation_payloads(payloads))
        reasoning = "".join(
            str(event.get("delta") or "")
            for event in events
            if event.get("type") == "conversation.reasoning.delta"
        )
        content = "".join(
            str(event.get("delta") or "")
            for event in events
            if event.get("type") == "conversation.delta"
        )

        self.assertEqual(reasoning, "思考了两秒")
        self.assertEqual(content, "最终答案")
        self.assertNotIn("private chain of thought", reasoning + content)

    def test_non_stream_exposes_reasoning_content(self) -> None:
        with (
            mock.patch.object(openai_v1_chat_complete, "text_backend", return_value=object()),
            mock.patch.object(
                openai_v1_chat_complete,
                "collect_text_output",
                return_value=TextCompletionOutput(content="42", reasoning_content="检查了计算"),
            ),
        ):
            response = openai_v1_chat_complete.text_chat_response(
                [{"role": "user", "content": "answer"}],
                "gpt-5.6-sol",
                "extended",
            )

        message = response["choices"][0]["message"]
        self.assertEqual(message["content"], "42")
        self.assertEqual(message["reasoning_content"], "检查了计算")

    def test_stream_exposes_reasoning_delta_before_content(self) -> None:
        with mock.patch.object(
            openai_v1_chat_complete,
            "stream_text_parts",
            return_value=iter([("reasoning", "检查"), ("reasoning", "完成"), ("content", "42")]),
        ):
            chunks = list(openai_v1_chat_complete.stream_text_chat_completion(
                object(),
                [{"role": "user", "content": "answer"}],
                "gpt-5.6-sol",
                "extended",
            ))

        deltas = [chunk["choices"][0]["delta"] for chunk in chunks]
        self.assertEqual(deltas[0], {"role": "assistant", "content": ""})
        self.assertEqual(deltas[1], {"reasoning_content": "检查"})
        self.assertEqual(deltas[2], {"reasoning_content": "完成"})
        self.assertEqual(deltas[3], {"content": "42"})
        self.assertEqual(chunks[-1]["choices"][0]["finish_reason"], "stop")


class ChatCompletionWebSearchTests(unittest.TestCase):
    def test_search_prompt_preserves_history_and_location(self) -> None:
        prompt = search_prompt_from_messages(
            [
                {"role": "system", "content": "Answer in Chinese."},
                {"role": "user", "content": "关注 OpenAI"},
                {"role": "assistant", "content": "好的。"},
                {"role": "user", "content": "今天有什么新消息？"},
            ],
            {
                "search_context_size": "high",
                "user_location": {"type": "approximate", "approximate": {"city": "上海", "country": "CN"}},
            },
        )

        self.assertIn("[SYSTEM] Answer in Chinese.", prompt)
        self.assertIn("[USER] 关注 OpenAI", prompt)
        self.assertIn("search context size: high", prompt)
        self.assertIn("user location: 上海, CN", prompt)
        self.assertTrue(prompt.endswith("今天有什么新消息？"))

    def test_search_stream_emits_role_before_blocking_search_and_annotations_after(self) -> None:
        body = {
            "model": "auto",
            "stream": True,
            "messages": [{"role": "user", "content": "latest news"}],
            "tools": [{"type": "web_search"}],
        }
        result = {
            "answer": "Latest answer.",
            "reasoning_content": "检索并核对了来源",
            "sources": [{"title": "Example", "url": "https://example.com/news", "snippet": ""}],
        }
        with mock.patch.object(openai_v1_chat_complete, "run_web_search", return_value=result) as search:
            stream = openai_v1_chat_complete.handle(body)
            first = next(stream)
            self.assertEqual(search.call_count, 0)
            rest = list(stream)

        self.assertEqual(first["choices"][0]["delta"]["role"], "assistant")
        self.assertEqual(search.call_count, 1)
        reasoning_chunks = [
            chunk for chunk in rest if "reasoning_content" in chunk["choices"][0]["delta"]
        ]
        self.assertEqual(
            reasoning_chunks[0]["choices"][0]["delta"]["reasoning_content"],
            "检索并核对了来源",
        )
        self.assertTrue(any("content" in chunk["choices"][0]["delta"] for chunk in rest))
        annotation_chunks = [
            chunk for chunk in rest if "annotations" in chunk["choices"][0]["delta"]
        ]
        self.assertEqual(
            annotation_chunks[0]["choices"][0]["delta"]["annotations"][0]["url_citation"]["url"],
            "https://example.com/news",
        )

    def test_search_result_uses_final_visible_assistant_message(self) -> None:
        conversation = {
            "mapping": {
                "tool-command": {"message": {
                    "author": {"role": "assistant"},
                    "recipient": "web.run",
                    "create_time": 4,
                    "content": {"content_type": "code", "text": "search(query)"},
                }},
                "reasoning": {"message": {
                    "author": {"role": "assistant"},
                    "recipient": "all",
                    "create_time": 5,
                    "content": {"content_type": "reasoning_recap", "content": "检索了多个来源"},
                }},
                "final": {"message": {
                    "id": "assistant-final",
                    "author": {"role": "assistant"},
                    "recipient": "all",
                    "channel": "final",
                    "create_time": 3,
                    "status": "finished_successfully",
                    "content": {"content_type": "text", "parts": ["Final answer"]},
                    "metadata": {
                        "finish_details": {"type": "stop"},
                        "content_references": [{"title": "Example", "url": "https://example.com"}],
                    },
                }},
            },
        }

        backend = OpenAIBackendAPI("token")
        try:
            result = backend._extract_search_result("conversation-1", conversation)
        finally:
            backend.close()

        self.assertEqual(result["answer"], "Final answer")
        self.assertEqual(result["reasoning_content"], "检索了多个来源")
        self.assertEqual(result["assistant_message_id"], "assistant-final")
        self.assertEqual(result["sources"][0]["url"], "https://example.com")

    def test_hosted_search_and_client_functions_are_rejected_when_mixed(self) -> None:
        with self.assertRaises(HTTPException) as raised:
            openai_v1_chat_complete.handle({
                "model": "auto",
                "messages": [{"role": "user", "content": "latest weather"}],
                "tools": [{"type": "web_search"}, WEATHER_TOOL],
            })

        self.assertEqual(raised.exception.status_code, 400)

    def test_run_web_search_closes_backend_and_uses_search_model_route(self) -> None:
        backend = mock.Mock()
        backend.search.return_value = {"answer": "ok", "sources": []}
        with (
            mock.patch.object(web_search_tool.account_service, "get_text_access_token", return_value="token") as token,
            mock.patch.object(web_search_tool.account_service, "mark_text_used") as mark,
            mock.patch.object(web_search_tool, "OpenAIBackendAPI", return_value=backend),
        ):
            result = web_search_tool.run_web_search("query")

        self.assertEqual(result["answer"], "ok")
        token.assert_called_once_with(model=web_search_tool.SEARCH_MODEL)
        backend.search.assert_called_once_with("query")
        backend.close.assert_called_once_with()
        mark.assert_called_once_with("token")


if __name__ == "__main__":
    unittest.main()
