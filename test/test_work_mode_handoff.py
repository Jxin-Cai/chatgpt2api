from __future__ import annotations

import json
import unittest
from unittest import mock

from services.openai_backend_api import ChatRequirements, OpenAIBackendAPI


class WorkModeHandoffTests(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = OpenAIBackendAPI(access_token="token")
        self.addCleanup(self.backend.close)

    def test_stream_handoff_polls_conversation_and_emits_one_final_done(self) -> None:
        response = mock.Mock()
        self.backend.session.post = mock.Mock(return_value=response)
        self.backend._bootstrap = mock.Mock()
        self.backend._get_chat_requirements = mock.Mock(return_value=ChatRequirements("requirements"))
        self.backend._conversation_payload = mock.Mock(return_value={})
        handoff = json.dumps({"type": "stream_handoff", "conversation_id": "conversation-1"})
        polled = json.dumps({"message": {"author": {"role": "assistant"}}})

        with (
            mock.patch("services.openai_backend_api.ensure_ok"),
            mock.patch(
                "services.openai_backend_api.iter_sse_payloads",
                return_value=iter([handoff, "[DONE]"]),
            ),
            mock.patch.object(
                self.backend,
                "_poll_handoff_conversation",
                return_value=iter([polled]),
            ) as poller,
        ):
            result = list(self.backend.stream_conversation(
                messages=[{"role": "user", "content": "hello"}],
                model="gpt-5.6-sol-wm",
            ))

        self.assertEqual(result, [handoff, polled, "[DONE]"])
        poller.assert_called_once_with("conversation-1")
        response.close.assert_called_once_with()

    def test_poll_handoff_emits_visible_final_message(self) -> None:
        in_progress = {
            "mapping": {
                "assistant": {
                    "message": {
                        "author": {"role": "assistant"},
                        "content": {"content_type": "text", "parts": [""]},
                        "status": "in_progress",
                        "recipient": "all",
                        "channel": "final",
                        "create_time": 1,
                    }
                }
            }
        }
        finished = {
            "mapping": {
                "assistant": {
                    "message": {
                        "author": {"role": "assistant"},
                        "content": {"content_type": "text", "parts": ["BRIDGE_OK"]},
                        "status": "finished_successfully",
                        "end_turn": True,
                        "recipient": "all",
                        "channel": "final",
                        "create_time": 1,
                    }
                }
            }
        }
        self.backend._get_conversation = mock.Mock(side_effect=[in_progress, finished])

        with mock.patch("services.openai_backend_api.time.sleep"):
            events = list(self.backend._poll_handoff_conversation("conversation-1"))

        event = json.loads(events[-1])
        self.assertEqual(event["message"]["content"]["parts"], ["BRIDGE_OK"])
        self.assertEqual(event["conversation_id"], "conversation-1")

    def test_poll_handoff_emits_reasoning_recap_before_final_message(self) -> None:
        finished = {
            "mapping": {
                "reasoning": {"message": {
                    "id": "reasoning-1",
                    "author": {"role": "assistant"},
                    "content": {"content_type": "reasoning_recap", "content": "思考了三秒"},
                    "status": "finished_successfully",
                    "recipient": "all",
                    "create_time": 1,
                }},
                "final": {"message": {
                    "id": "final-1",
                    "author": {"role": "assistant"},
                    "content": {"content_type": "text", "parts": ["BRIDGE_OK"]},
                    "status": "finished_successfully",
                    "end_turn": True,
                    "recipient": "all",
                    "channel": "final",
                    "create_time": 2,
                }},
            },
        }
        self.backend._get_conversation = mock.Mock(return_value=finished)

        events = [json.loads(event) for event in self.backend._poll_handoff_conversation("conversation-1")]

        self.assertEqual(events[0]["message"]["content"]["content_type"], "reasoning_recap")
        self.assertEqual(events[1]["message"]["content"]["parts"], ["BRIDGE_OK"])

    def test_poll_handoff_does_not_finish_on_previous_turn_assistant(self) -> None:
        waiting = {
            "current_node": "current-user",
            "mapping": {
                "old-user": {"parent": None, "message": {
                    "id": "old-user",
                    "author": {"role": "user"},
                    "content": {"content_type": "text", "parts": ["first"]},
                }},
                "old-assistant": {"parent": "old-user", "message": {
                    "id": "old-assistant",
                    "author": {"role": "assistant"},
                    "content": {"content_type": "text", "parts": ["OLD"]},
                    "status": "finished_successfully",
                    "end_turn": True,
                    "recipient": "all",
                }},
                "current-user": {"parent": "old-assistant", "message": {
                    "id": "current-user",
                    "author": {"role": "user"},
                    "content": {"content_type": "text", "parts": ["second"]},
                }},
            },
        }
        finished = {
            **waiting,
            "current_node": "current-final",
            "mapping": {
                **waiting["mapping"],
                "current-final": {"parent": "current-user", "message": {
                    "id": "current-final",
                    "author": {"role": "assistant"},
                    "content": {"content_type": "text", "parts": ["NEW"]},
                    "status": "finished_successfully",
                    "end_turn": True,
                    "recipient": "all",
                    "channel": "final",
                }},
            },
        }
        self.backend._get_conversation = mock.Mock(side_effect=[waiting, finished])

        with mock.patch("services.openai_backend_api.time.sleep"):
            events = [json.loads(event) for event in self.backend._poll_handoff_conversation("conversation-1")]

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["message"]["content"]["parts"], ["NEW"])

    def test_web_reasoning_effort_is_mapped_to_advertised_values(self) -> None:
        self.assertEqual(self.backend._normalize_thinking_effort("low"), "standard")
        self.assertEqual(self.backend._normalize_thinking_effort("medium"), "standard")
        self.assertEqual(self.backend._normalize_thinking_effort("high"), "extended")
        self.assertEqual(self.backend._normalize_thinking_effort("xhigh"), "extended")
        self.assertEqual(self.backend._normalize_thinking_effort("max"), "extended")

    def test_conversation_payload_requests_visible_reasoning_recap(self) -> None:
        payload = self.backend._conversation_payload(
            [{"role": "user", "content": "hello"}],
            "gpt-5-6-thinking",
            "Asia/Shanghai",
            thinking_effort="high",
        )

        self.assertEqual(payload["thinking_effort"], "extended")
        self.assertEqual(payload["paragen_cot_summary_display_override"], "allow")


if __name__ == "__main__":
    unittest.main()
