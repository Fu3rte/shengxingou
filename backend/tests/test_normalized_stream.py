import unittest

from openai.types.chat import ChatCompletionChunk

from src.ai.models.normalized import async_normalized_stream
from tests.stream_helpers import async_chunks, sync_stream


def normalized_stream(chunks, secrets):
    return sync_stream(async_normalized_stream(async_chunks(chunks), secrets))


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

    def test_interleaved_text_matches_final_message(self):
        chunks = [chunk({"role": "assistant", "content": "a", "refusal": "x"}),
                  chunk({"content": "b", "refusal": "y"}), chunk({}, "stop")]
        events = list(normalized_stream(chunks, []))
        text = "".join(event["data"]["fragments"].get("text", "") for event in events[:-1])
        self.assertEqual(text, "abxy")
        self.assertEqual(text, "".join(block["text"] for block in events[-1]["data"]["message"]["content"]))

    def test_incomplete_response_retains_reason_and_usage(self):
        usage = ChatCompletionChunk.model_validate({
            "id": "response", "object": "chat.completion.chunk", "created": 1, "model": "example", "choices": [],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5, "vendor": "ignored"},
        })
        for reason in ("length", "content_filter"):
            events = normalized_stream([chunk({"role": "assistant", "content": "partial"}),
                                        chunk({}, reason), usage], [])
            self.assertEqual(next(events)["data"]["fragments"], {"text": "partial"})
            response = next(events)["data"]
            self.assertEqual(response, {"message": None, "finish_reason": reason,
                                        "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}})
            with self.assertRaises(ValueError):
                next(events)

    def test_incomplete_refusal_records_safe_fragments_in_text_order(self):
        for reason in ("length", "content_filter"):
            chunks = [chunk({"role": "assistant", "content": "a", "refusal": "x", "vendor": "ignored"}),
                      chunk({"content": "b", "refusal": "y"}), chunk({}, reason)]
            events = normalized_stream(chunks, ["secret"])
            with self.subTest(reason=reason):
                self.assertEqual([next(events)["data"]["fragments"] for _ in range(4)],
                                 [{"text": "a"}, {"text": "b"}, {"text": "x"}, {"text": "y"}])
                self.assertEqual(next(events)["data"], {"message": None, "finish_reason": reason, "usage": None})
                with self.assertRaises(ValueError):
                    next(events)

    def test_inconsistent_response_is_recorded_without_message(self):
        events = normalized_stream([chunk({"role": "assistant", "content": "text"}),
                                    chunk({}, "tool_calls")], [])
        self.assertEqual(next(events)["data"]["fragments"], {"text": "text"})
        self.assertEqual(next(events)["data"], {"message": None, "finish_reason": "tool_calls", "usage": None})
        with self.assertRaises(ValueError):
            next(events)

    def test_interrupted_stream_records_available_information(self):
        def interrupted():
            yield chunk({"role": "assistant", "content": "partial"})
            raise ConnectionError("interrupted")

        events = normalized_stream(interrupted(), [])
        self.assertEqual(next(events)["data"]["fragments"], {"text": "partial"})
        self.assertEqual(next(events)["data"], {"message": None, "finish_reason": None, "usage": None})
        with self.assertRaises(ConnectionError):
            next(events)

    def test_interrupted_refusal_records_safe_fragments(self):
        def interrupted():
            yield chunk({"role": "assistant", "content": "content ", "refusal": "safe refusal"})
            raise ConnectionError("interrupted")

        events = normalized_stream(interrupted(), ["secret"])
        self.assertEqual(next(events)["data"]["fragments"], {"text": "content "})
        self.assertEqual(next(events)["data"]["fragments"], {"text": "safe refusal"})
        self.assertEqual(next(events)["data"], {"message": None, "finish_reason": None, "usage": None})
        with self.assertRaisesRegex(ConnectionError, "interrupted"):
            next(events)

    def test_refusal_and_tool_conflict_records_safe_refusal(self):
        chunks = [chunk({"role": "assistant", "refusal": "safe refusal"}),
                  chunk({"tool_calls": [{"index": 0, "id": "call1", "type": "function",
                                         "function": {"name": "todo", "arguments": "{}"}}]}),
                  chunk({}, "tool_calls")]
        events = normalized_stream(chunks, ["secret"])
        fragments = [next(events)["data"]["fragments"] for _ in range(3)]
        self.assertEqual(fragments[-1], {"text": "safe refusal"})
        self.assertEqual(next(events)["data"], {"message": None, "finish_reason": "tool_calls", "usage": None})
        with self.assertRaises(ValueError):
            next(events)

    def test_incomplete_refusal_secret_is_not_released(self):
        chunks = [chunk({"role": "assistant", "content": "safe ", "refusal": "fine "}),
                  chunk({"refusal": "sec"}), chunk({"refusal": "ret"}), chunk({}, "length")]
        events = normalized_stream(chunks, ["secret"])
        self.assertEqual(next(events)["data"]["fragments"], {"text": "safe "})
        self.assertEqual(next(events)["data"]["fragments"], {"text": "fine "})
        self.assertEqual(next(events)["data"], {"message": None, "finish_reason": "length", "usage": None})
        with self.assertRaisesRegex(ValueError, "未完成"):
            next(events)

    def test_interrupted_content_and_refusal_secret_is_not_released(self):
        def interrupted():
            yield chunk({"role": "assistant", "content": "sec", "refusal": "ret"})
            raise ConnectionError("interrupted")

        events = normalized_stream(interrupted(), ["secret"])
        self.assertEqual(next(events)["data"], {"message": None, "finish_reason": None, "usage": None})
        with self.assertRaisesRegex(ConnectionError, "interrupted"):
            next(events)

    def test_interrupted_refusal_secret_prefix_is_not_released(self):
        def interrupted():
            yield chunk({"role": "assistant", "refusal": "sec"})
            raise ConnectionError("interrupted")

        events = normalized_stream(interrupted(), ["secret"])
        self.assertEqual(next(events)["data"], {"message": None, "finish_reason": None, "usage": None})
        with self.assertRaisesRegex(ConnectionError, "interrupted"):
            next(events)

    def test_interrupted_secret_in_response_metadata_preserves_original_error(self):
        error = ConnectionError("interrupted")

        def interrupted():
            yield chunk({"role": "assistant", "refusal": "safe refusal"})
            raise error

        events = normalized_stream(interrupted(), ["message"])
        self.assertEqual(next(events)["data"]["fragments"], {"text": "safe refusal"})
        with self.assertRaises(ConnectionError) as raised:
            next(events)
        self.assertIs(raised.exception, error)

    def test_incomplete_secret_in_finish_reason_preserves_original_error(self):
        events = normalized_stream([chunk({"role": "assistant", "refusal": "safe!"}),
                                    chunk({}, "length")], ["length"])
        self.assertEqual(next(events)["data"]["fragments"], {"text": "safe!"})
        with self.assertRaisesRegex(ValueError, "未完成"):
            next(events)

    def test_interrupted_secret_in_fragment_key_preserves_original_error(self):
        error = ConnectionError("interrupted")

        def interrupted():
            yield chunk({"role": "assistant", "refusal": "safe refusal"})
            raise error

        events = normalized_stream(interrupted(), ["text"])
        self.assertEqual(next(events)["data"], {"message": None, "finish_reason": None, "usage": None})
        with self.assertRaises(ConnectionError) as raised:
            next(events)
        self.assertIs(raised.exception, error)

    def test_split_content_secret_is_not_released(self):
        chunks = [chunk({"role": "assistant", "content": "safe "}),
                  chunk({"content": "sec"}), chunk({"content": "ret"}), chunk({}, "stop")]
        events = normalized_stream(chunks, ["secret"])
        self.assertEqual(next(events)["data"]["fragments"], {"text": "safe "})
        self.assertIsNone(next(events)["data"]["message"])
        with self.assertRaisesRegex(ValueError, "密钥"):
            next(events)

    def test_secret_split_between_content_and_refusal(self):
        chunks = [chunk({"role": "assistant", "content": "sec"}),
                  chunk({"refusal": "ret"}), chunk({}, "stop")]
        with self.assertRaisesRegex(ValueError, "密钥"):
            list(normalized_stream(chunks, ["secret"]))

    def test_parallel_tool_fragments_keep_indices(self):
        chunks = [chunk({"role": "assistant", "tool_calls": [
            {"index": 0, "id": "a", "type": "function", "function": {"name": "search", "arguments": '{"query":"'}},
            {"index": 1, "id": "b", "type": "function", "function": {"name": "search", "arguments": '{"query":"'}},
        ]}), chunk({"tool_calls": [
            {"index": 1, "function": {"arguments": 'second"}'}},
            {"index": 0, "function": {"arguments": 'first"}'}},
        ]}), chunk({}, "tool_calls")]
        events = list(normalized_stream(chunks, ["secret"]))
        fragments = [event["data"]["fragments"] for event in events[:-1]]
        for index, value in enumerate(("first", "second")):
            key = f"tool:{index}:arguments"
            self.assertEqual("".join(fragment.get(key, "") for fragment in fragments), '{"query":"' + value + '"}')
            self.assertEqual(events[-1]["data"]["message"]["content"][index]["arguments"], '{"query":"' + value + '"}')

    def test_escaped_secret_in_tool_arguments_is_not_released(self):
        chunks = [
            chunk({"role": "assistant", "tool_calls": [{"index": 0, "id": "call1", "type": "function",
                                                         "function": {"name": "search", "arguments": '{"query":"\\u00'}}]}),
            chunk({"tool_calls": [{"index": 0, "function": {"arguments": '73ecret"}'}}]}),
            chunk({}, "tool_calls"),
        ]
        events = normalized_stream(chunks, ["secret"])
        self.assertEqual(next(events)["data"]["fragments"], {"tool:0:id": "call1"})
        self.assertEqual(next(events)["data"]["fragments"], {"tool:0:name": "search"})
        self.assertIsNone(next(events)["data"]["message"])
        with self.assertRaisesRegex(ValueError, "密钥"):
            next(events)

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
        self.assertIsNone(next(events)["data"]["message"])
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
        self.assertIsNone(next(events)["data"]["message"])
        with self.assertRaisesRegex(ValueError, "密钥"):
            next(events)


class AsyncNormalizedStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_async_stream_matches_sync_and_checks_split_secret(self):
        chunks = [chunk({"role": "assistant", "content": "hello "}),
                  chunk({"content": "world"}), chunk({}, "stop")]

        async def source(items):
            for item in items:
                yield item

        events = [event async for event in async_normalized_stream(source(chunks), ["secret"])]
        self.assertEqual([event["data"]["fragments"] for event in events[:-1]],
                         [{"text": "hello "}, {"text": "world"}])
        self.assertEqual(events[-1]["data"]["message"]["content"][0]["text"], "hello world")

        chunks = [chunk({"role": "assistant", "content": "safe "}),
                  chunk({"content": "sec"}), chunk({"content": "ret"}), chunk({}, "stop")]
        events = async_normalized_stream(source(chunks), ["secret"])
        self.assertEqual((await anext(events))["data"]["fragments"], {"text": "safe "})
        self.assertIsNone((await anext(events))["data"]["message"])
        with self.assertRaisesRegex(ValueError, "密钥"):
            await anext(events)

        chunks = [chunk({"role": "assistant", "content": "a", "refusal": "x"}), chunk({}, "length")]
        events = async_normalized_stream(source(chunks), ["secret"])
        self.assertEqual((await anext(events))["data"]["fragments"], {"text": "a"})
        self.assertEqual((await anext(events))["data"]["fragments"], {"text": "x"})
        self.assertEqual((await anext(events))["data"]["finish_reason"], "length")
        with self.assertRaises(ValueError):
            await anext(events)


if __name__ == "__main__":
    unittest.main()
