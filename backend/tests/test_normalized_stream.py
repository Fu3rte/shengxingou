import unittest

from openai.types.chat import ChatCompletionChunk

from src.ai.models.normalized import normalized_stream


def chunk(delta, finish_reason=None, **extra):
    return ChatCompletionChunk.model_validate({
        "id": "response", "object": "chat.completion.chunk", "created": 1, "model": "example",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}], **extra,
    })


class NormalizedStreamTests(unittest.TestCase):
    def test_normalized_events_omit_extensions_and_preserve_text(self):
        chunks = [
            chunk({"role": "assistant", "content": "hello ", "vendor": "ignored"}, vendor="ignored"),
            chunk({"content": "world"}), chunk({}, "stop"),
        ]
        events = list(normalized_stream(chunks, ["secret"]))
        self.assertEqual(events[0], {"type": "model.delta", "data": {"fragments": {"text": "hello "}}})
        self.assertEqual(events[1], {"type": "model.delta", "data": {"fragments": {"text": "world"}}})
        self.assertEqual(events[-1]["data"]["message"]["content"][0]["text"], "hello world")
        self.assertNotIn("vendor", str(events))

    def test_available_usage_details_are_normalized(self):
        usage_chunk = ChatCompletionChunk.model_validate({
            "id": "response", "object": "chat.completion.chunk", "created": 1, "model": "example", "choices": [],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5,
                      "prompt_tokens_details": {"cached_tokens": 1, "vendor": "ignored"}},
        })
        events = list(normalized_stream([chunk({"role": "assistant", "content": "ok"}),
                                         chunk({}, "stop"), usage_chunk], []))
        self.assertEqual(events[-1]["data"]["usage"], {"prompt_tokens": 3, "completion_tokens": 2,
                         "total_tokens": 5, "prompt_tokens_details": {"cached_tokens": 1}})

    def test_split_content_secret_is_not_released(self):
        chunks = [chunk({"role": "assistant", "content": "safe "}),
                  chunk({"content": "sec"}), chunk({"content": "ret"}), chunk({}, "stop")]
        events = normalized_stream(chunks, ["secret"])
        self.assertEqual(next(events)["data"]["fragments"], {"text": "safe "})
        with self.assertRaisesRegex(ValueError, "密钥"):
            next(events)

    def test_secret_split_between_content_and_refusal(self):
        chunks = [chunk({"role": "assistant", "content": "sec"}),
                  chunk({"refusal": "ret"}), chunk({}, "stop")]
        with self.assertRaisesRegex(ValueError, "密钥"):
            list(normalized_stream(chunks, ["secret"]))

    def test_split_tool_argument_secret_is_not_released(self):
        chunks = [
            chunk({"role": "assistant", "tool_calls": [{"index": 0, "id": "call1", "type": "function",
                                                         "function": {"name": "todo", "arguments": "sec"}}]}),
            chunk({"tool_calls": [{"index": 0, "function": {"arguments": "ret"}}]}),
            chunk({}, "tool_calls"),
        ]
        events = normalized_stream(chunks, ["secret"])
        self.assertEqual(next(events)["data"]["fragments"], {"tool:0:id": "call1"})
        self.assertEqual(next(events)["data"]["fragments"], {"tool:0:name": "todo"})
        with self.assertRaisesRegex(ValueError, "密钥"):
            next(events)

    def test_repeated_tool_index_in_one_chunk(self):
        chunks = [chunk({"role": "assistant", "tool_calls": [
            {"index": 0, "id": "call1", "type": "function", "function": {"name": "todo", "arguments": "sec"}},
            {"index": 0, "function": {"arguments": "ret"}},
        ]}), chunk({}, "tool_calls")]
        events = normalized_stream(chunks, ["secret"])
        self.assertEqual(next(events)["data"]["fragments"], {"tool:0:id": "call1"})
        self.assertEqual(next(events)["data"]["fragments"], {"tool:0:name": "todo"})
        with self.assertRaisesRegex(ValueError, "密钥"):
            next(events)


if __name__ == "__main__":
    unittest.main()
