from __future__ import annotations

from io import BytesIO
import unittest
from unittest import mock

from fastapi import HTTPException
from PIL import Image

from api.image_inputs import _parse_count
from services.protocol import openai_v1_chat_complete as chat
from services.protocol.conversation import ImageGenerationError, ImageOutput
from services.protocol.openai_v1_image_edit import _composite_mask


def picture(mode="RGBA", size=(2, 2), color=(255, 0, 0, 255), name="input.jpg"):
    buffer = BytesIO()
    Image.new(mode, size, color).save(buffer, "PNG")
    return buffer.getvalue(), name, "image/png"


class StreamUsageTests(unittest.TestCase):
    def test_usage_follows_finish_and_preserves_cached_chunks(self):
        messages = [{"role": "user", "content": "hello"}]
        chunks = [
            chat.completion_chunk("auto", {"content": "answer"}, completion_id="chatcmpl-test", created=123),
            chat.completion_chunk("auto", {"reasoning_content": "thinking"}, completion_id="chatcmpl-test", created=123),
            chat.completion_chunk("auto", {}, "stop", "chatcmpl-test", 123),
        ]
        result = list(chat.stream_with_usage(chunks, messages, "auto"))
        self.assertTrue(all("usage" not in c for c in chunks))
        self.assertTrue(all(c["usage"] is None for c in result[:-1]))
        self.assertEqual(result[-2]["choices"][0]["finish_reason"], "stop")
        self.assertEqual(result[-1]["choices"], [])
        self.assertEqual({c["id"] for c in result}, {"chatcmpl-test"})
        self.assertEqual({c["created"] for c in result}, {123})
        expected = chat.completion_response("auto", "answer", messages=messages, reasoning_content="thinking")["usage"]
        self.assertEqual(result[-1]["usage"], expected)
        self.assertGreater(expected["completion_tokens_details"]["reasoning_tokens"], 0)
        self.assertEqual(expected["total_tokens"], expected["prompt_tokens"] + expected["completion_tokens"])

    def test_interrupted_stream_has_no_success_usage(self):
        def broken():
            yield chat.completion_chunk("auto", {"content": "partial"})
            raise RuntimeError("upstream failed")
        stream = chat.stream_with_usage(broken(), [], "auto")
        self.assertIsNone(next(stream)["usage"])
        with self.assertRaisesRegex(RuntimeError, "upstream failed"):
            next(stream)

    def test_tool_arguments_are_counted(self):
        chunks = [chat.completion_chunk("auto", {"tool_calls": [
            {"index": 0, "function": {"name": "weather", "arguments": '{"city":'}},
            {"index": 0, "function": {"arguments": '"Shanghai"}'}},
        ]}, "tool_calls")]
        result = list(chat.stream_with_usage(chunks, [{"role": "user", "content": "weather?"}], "auto"))
        self.assertGreater(result[-1]["usage"]["completion_tokens"], 0)

    def test_handle_usage_opt_in(self):
        body = {"model": "auto", "messages": [{"role": "user", "content": "hello"}], "stream": True}
        for enabled in (False, True):
            with self.subTest(enabled=enabled), mock.patch.object(chat, "_handle", return_value=iter([
                chat.completion_chunk("auto", {}, "stop")
            ])):
                result = list(chat.handle({**body, "stream_options": {"include_usage": enabled}}))
                self.assertEqual(len(result), 2 if enabled else 1)

    def test_invalid_options_rejected_before_generation(self):
        for options in ([], "yes", {"include_usage": "false"}):
            with self.subTest(options=options), mock.patch.object(chat, "_handle") as dispatch:
                with self.assertRaises(HTTPException):
                    chat.handle({"stream": True, "stream_options": options})
                dispatch.assert_not_called()

    def test_image_chat_usage_avoids_counting_base64_as_text(self):
        body = {"stream": True, "stream_options": {"include_usage": True}}
        with (
            mock.patch.object(chat, "chat_image_args", return_value=("gpt-image-2.5", "draw", 1, [])),
            mock.patch.object(chat, "stream_image_outputs_with_pool", return_value=iter([
                ImageOutput(kind="result", model="gpt-image-2.5", index=1, total=1, data=[{"b64_json": "fake"}])
            ])),
            mock.patch.object(chat, "count_image_output_items_tokens", return_value=42),
        ):
            result = list(chat.image_chat_events(body))
        self.assertEqual(result[-1]["choices"], [])
        self.assertEqual(result[-1]["usage"]["completion_tokens"], 42)


class MaskCompatibilityTests(unittest.TestCase):
    def test_single_mask_only_changes_first_image(self):
        images = [picture(), picture(color=(0, 255, 0, 255))]
        mask = picture(color=(0, 0, 0, 0))
        result = _composite_mask(images, [mask])
        self.assertEqual(result[1], images[1])
        self.assertEqual(result[0][1:], ("input.png", "image/png"))
        with Image.open(BytesIO(result[0][0])) as image:
            self.assertEqual(image.getchannel("A").getextrema(), (0, 0))

    def test_invalid_masks_return_client_error(self):
        for mask in (picture(size=(1, 1)), (b"broken", "mask.png", "image/png"), picture("RGB", color=(0, 0, 0))):
            with self.subTest(mask=mask[1:]):
                with self.assertRaises(ImageGenerationError) as caught:
                    _composite_mask([picture()], [mask])
                self.assertEqual(caught.exception.status_code, 400)

    def test_grayscale_paired_masks_remain_supported(self):
        result = _composite_mask([picture(), picture()], [picture("L", color=0), picture("L", color=255)])
        for item, expected in zip(result, (0, 255)):
            with Image.open(BytesIO(item[0])) as image:
                self.assertEqual(image.getchannel("A").getextrema(), (expected, expected))

    def test_count_rejects_zero_fraction_boolean(self):
        for value in (0, False, True, 1.5, "1.5", -1, 5):
            with self.subTest(value=value), self.assertRaises(HTTPException):
                _parse_count(value)
        for value, expected in ((None, 1), ("", 1), ("2", 2), (4, 4)):
            self.assertEqual(_parse_count(value), expected)


if __name__ == "__main__":
    unittest.main()
