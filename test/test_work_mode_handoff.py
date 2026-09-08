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


if __name__ == "__main__":
    unittest.main()
