import asyncio
import unittest

from openai.types.chat import ChatCompletion, ChatCompletionChunk
from pydantic import TypeAdapter, ValidationError

from src.agent.types import (
    AgentMessage,
    AssistantMessage,
    CustomMessage,
    SystemMessage,
    TextContent,
    ToolCallContent,
    ToolResultMessage,
    UserMessage,
)
from src.ai.models.openai import ModelResponse, async_convert_stream, from_openai_response, to_openai_messages
from tests.stream_helpers import async_chunks, sync_stream


def convert_stream(chunks):
    return sync_stream(async_convert_stream(async_chunks(chunks)))


def chunk(choices, **extra):
    return ChatCompletionChunk.model_validate({
        "id": "response-1", "object": "chat.completion.chunk", "created": 1,
        "model": "example", "choices": choices, **extra,
    })


def choice(delta, finish_reason=None, index=0):
    return {"index": index, "delta": delta, "finish_reason": finish_reason}


def response(message, finish_reason="stop"):
    return ChatCompletion.model_validate({
        "id": "response-1", "object": "chat.completion", "created": 1,
        "model": "example", "choices": [{"index": 0, "finish_reason": finish_reason, "message": message}],
    })


class MessageConversionTests(unittest.TestCase):
    def test_json_roundtrip_and_custom_json(self):
        adapter = TypeAdapter(list[AgentMessage])
        original = [
            SystemMessage(content="规则"), UserMessage(content="你好"),
            AssistantMessage(content=[TextContent(text="a"), ToolCallContent(id="c1", name="f", arguments=' {"x": 1} ')]),
            ToolResultMessage(tool_call_id="c1", content="结果"),
            CustomMessage(type="note", data={"values": [None, True, 1, "x"]}),
        ]
        self.assertEqual(adapter.validate_json(adapter.dump_json(original)), original)
        with self.assertRaises(ValidationError):
            adapter.validate_json('[{"role":"unknown"}]')
        with self.assertRaises(ValidationError):
            CustomMessage(type="note", data=object())
        with self.assertRaises(ValidationError):
            AssistantMessage.model_validate({"content": [{"type": "image", "url": "x"}]})

    def test_request_conversion_and_roundtrip(self):
        assistant = AssistantMessage(content=[
            TextContent(text="a"), ToolCallContent(id="1", name="first", arguments=' {"x": 1} '),
            TextContent(text="b"), ToolCallContent(id="2", name="second", arguments="not-json"),
        ])
        messages = [SystemMessage(content="s"), UserMessage(content="u"), assistant,
                    CustomMessage(type="event", data=None), ToolResultMessage(tool_call_id="2", content="two"),
                    ToolResultMessage(tool_call_id="1", content="one")]
        converted = to_openai_messages(messages)
        self.assertEqual([m["role"] for m in converted], ["system", "user", "assistant", "tool", "tool"])
        self.assertEqual(converted[2]["content"], "ab")
        self.assertEqual([c["function"]["arguments"] for c in converted[2]["tool_calls"]], [' {"x": 1} ', "not-json"])
        openai_message = {**converted[2], "refusal": None}
        restored = from_openai_response(response(openai_message, "tool_calls"))
        self.assertEqual(restored.content, [TextContent(text="ab"), assistant.content[1], assistant.content[3]])
        only_tool = AssistantMessage(content=[ToolCallContent(id="1", name="f", arguments="{}")])
        self.assertIsNone(to_openai_messages([only_tool, ToolResultMessage(tool_call_id="1", content="ok")])[0]["content"])

    def test_tool_id_pairing(self):
        call = ToolCallContent(id="1", name="f", arguments="{}")
        cases = [
            [AssistantMessage(content=[call])],
            [ToolResultMessage(tool_call_id="1", content="x")],
            [AssistantMessage(content=[call, call])],
            [AssistantMessage(content=[call]), ToolResultMessage(tool_call_id="2", content="x")],
            [AssistantMessage(content=[call]), UserMessage(content="u"), ToolResultMessage(tool_call_id="1", content="x")],
            [AssistantMessage(content=[call]), ToolResultMessage(tool_call_id="1", content="x"), ToolResultMessage(tool_call_id="1", content="x")],
        ]
        for messages in cases:
            with self.subTest(messages=messages), self.assertRaises(ValueError):
                to_openai_messages(messages)
        with self.assertRaises(ValueError):
            to_openai_messages([{"role": "unknown"}])

    def test_response_refusal_and_finish_validation(self):
        msg = from_openai_response(response({"role": "assistant", "content": "text", "refusal": "refused"}))
        self.assertEqual(msg.content, [TextContent(text="text"), TextContent(text="refused")])
        calls = [{"id": "1", "type": "function", "function": {"name": "f", "arguments": "{}"}}]
        cases = [
            ({"role": "assistant", "content": "x", "tool_calls": calls}, "stop"),
            ({"role": "assistant", "content": "x"}, "tool_calls"),
            ({"role": "assistant", "refusal": "no", "tool_calls": calls}, "tool_calls"),
            ({"role": "assistant", "content": "x"}, "length"),
            ({"role": "assistant", "content": "x"}, "content_filter"),
            ({"role": "assistant", "content": None}, "stop"),
            ({"role": "assistant", "tool_calls": [{"id": "1", "type": "custom", "custom": {"name": "f", "input": "x"}}]}, "tool_calls"),
        ]
        for message, reason in cases:
            with self.subTest(reason=reason, message=message), self.assertRaises(ValueError):
                from_openai_response(response(message, reason))
        invalid = response({"role": "assistant", "content": "x"})
        invalid.choices = []
        with self.assertRaises(ValueError):
            from_openai_response(invalid)

    def test_stream_split_tools_usage_and_raw_fields(self):
        chunks = [
            chunk([choice({"role": "assistant", "content": "a", "vendor_delta": {"extra": True}})], vendor={"flag": 1}),
            chunk([choice({"content": "b", "tool_calls": [
                {"index": 0, "id": "1", "type": "function", "function": {"name": "f", "arguments": '{"a":'}},
                {"index": 1, "id": "2", "type": "function", "function": {"name": "g", "arguments": "invalid-"}},
            ]})]),
            chunk([choice({"tool_calls": [
                {"index": 0, "function": {"arguments": " 1}"}},
                {"index": 1, "function": {"arguments": "json"}},
            ]})]),
            chunk([choice({}, "tool_calls")]),
            chunk([], usage={"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}, vendor_usage="kept"),
        ]
        stream = convert_stream(iter(chunks))
        for source in chunks:
            self.assertEqual(next(stream), source.model_dump(mode="json", exclude_unset=True))
        final = next(stream)
        self.assertIsInstance(final, ModelResponse)
        self.assertEqual(final.finish_reason, "tool_calls")
        self.assertEqual(final.response.usage.total_tokens, 5)
        self.assertEqual(final.message.content, [TextContent(text="ab"),
            ToolCallContent(id="1", name="f", arguments='{"a": 1}'),
            ToolCallContent(id="2", name="g", arguments="invalid-json")])
        with self.assertRaises(StopIteration):
            next(stream)

        async def convert():
            async def source():
                for item in chunks:
                    yield item
            return [item async for item in async_convert_stream(source())]

        converted = asyncio.run(convert())
        self.assertEqual(converted[:-1], [item.model_dump(mode="json", exclude_unset=True) for item in chunks])
        self.assertEqual(converted[-1], final)

    def test_stream_refusal_split_and_immediate_delivery_on_error(self):
        chunks = [chunk([choice({"role": "assistant", "content": "hello ", "refusal": "can"})]),
                  chunk([choice({"content": "world", "refusal": "not"})]),
                  chunk([choice({}, "stop")])]
        result = list(convert_stream(chunks))[-1]
        self.assertEqual(result.message.content, [TextContent(text="hello world"), TextContent(text="cannot")])
        bad = chunk([choice({"role": "user"})], unexpected="preserved")
        stream = convert_stream([bad])
        self.assertEqual(next(stream)["unexpected"], "preserved")
        with self.assertRaises(ValueError):
            next(stream)
        invalid_index = chunk([choice({"role": "assistant"}, index=2)])
        stream = convert_stream([invalid_index])
        self.assertEqual(next(stream), invalid_index.model_dump(mode="json", exclude_unset=True))
        with self.assertRaises(IndexError):
            next(stream)

    def test_stream_refusal_with_tool_and_unknown_type(self):
        for tool_type in ("function", "custom"):
            chunks = [
                chunk([choice({"role": "assistant", "refusal": "no"})]),
                chunk([choice({"tool_calls": [{"index": 0, "id": "1", "type": "function",
                                             "function": {"name": "f", "arguments": "{}"}}]})]),
                chunk([choice({}, "tool_calls")]),
            ]
            chunks[1].choices[0].delta.tool_calls[0].type = tool_type
            stream = convert_stream(chunks)
            self.assertEqual(next(stream), chunks[0].model_dump(mode="json", exclude_unset=True))
            self.assertEqual(next(stream), chunks[1].model_dump(mode="json", exclude_unset=True))
            if tool_type == "function":
                self.assertEqual(next(stream), chunks[2].model_dump(mode="json", exclude_unset=True))
            with self.assertRaises(ValueError):
                next(stream)

    def test_stream_interruption_preserves_delivered_chunk(self):
        first = chunk([choice({"role": "assistant", "content": "partial"})])

        def interrupted():
            yield first
            raise ConnectionError("stream interrupted")

        stream = convert_stream(interrupted())
        self.assertEqual(next(stream), first.model_dump(mode="json", exclude_unset=True))
        with self.assertRaises(ConnectionError):
            next(stream)

    def test_stream_rejects_content_and_conflicting_finish_after_terminal(self):
        call = {"index": 0, "id": "1", "type": "function", "function": {"name": "f", "arguments": '{"x":'}}
        continued_call = {"index": 0, "function": {"arguments": "1}"}}
        cases = [
            ("length", {"tool_calls": [continued_call]}, "tool_calls"),
            ("length", {}, "tool_calls"),
            ("content_filter", {}, "tool_calls"),
            ("stop", {"content": "after"}, None),
            ("stop", {"content": ""}, None),
            ("stop", {"vendor_delta": "after"}, None),
            ("tool_calls", {"refusal": "after"}, None),
        ]
        for first_reason, delta, second_reason in cases:
            initial = {"role": "assistant", "content": "x"}
            if first_reason != "stop":
                initial["tool_calls"] = [call]
            chunks = [chunk([choice(initial)]), chunk([choice({}, first_reason)]),
                      chunk([choice(delta, second_reason)], vendor="kept")]
            stream = convert_stream(chunks)
            with self.subTest(first_reason=first_reason, delta=delta, second_reason=second_reason):
                for source in chunks:
                    self.assertEqual(next(stream), source.model_dump(mode="json", exclude_unset=True))
                with self.assertRaisesRegex(ValueError, "模型结束后"):
                    next(stream)

    def test_stream_terminal_rejects_late_logprobs(self):
        late = choice({})
        late["logprobs"] = {"content": []}
        chunks = [chunk([choice({"role": "assistant", "content": "x"}, "stop")]), chunk([late])]
        stream = convert_stream(chunks)
        for source in chunks:
            self.assertEqual(next(stream), source.model_dump(mode="json", exclude_unset=True))
        with self.assertRaisesRegex(ValueError, "模型结束后"):
            next(stream)

    def test_stream_terminal_allows_usage_only_chunk(self):
        usage = {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}
        for reason in ("stop", "length", "content_filter"):
            chunks = [chunk([choice({"role": "assistant", "content": "text"})]),
                      chunk([choice({}, reason)]), chunk([], usage=usage)]
            stream = convert_stream(chunks)
            with self.subTest(reason=reason):
                for source in chunks:
                    self.assertEqual(next(stream), source.model_dump(mode="json", exclude_unset=True))
                if reason == "stop":
                    self.assertEqual(next(stream).response.usage.total_tokens, 5)
                else:
                    with self.assertRaises(ValueError):
                        next(stream)

    def test_stream_terminal_allows_empty_choice_and_repeated_reason(self):
        chunks = [chunk([choice({"role": "assistant", "content": "text"}, "stop")]),
                  chunk([choice({}, None)]), chunk([choice({}, "stop")]),
                  chunk([], usage={"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5})]
        stream = convert_stream(chunks)
        for source in chunks:
            self.assertEqual(next(stream), source.model_dump(mode="json", exclude_unset=True))
        result = next(stream)
        self.assertEqual(result.message.content, [TextContent(text="text")])
        self.assertEqual(result.response.usage.total_tokens, 5)

    def test_stream_incomplete_and_choice_count(self):
        for reason in ("length", "content_filter", None):
            chunks = [chunk([choice({"role": "assistant", "content": "partial"})]), chunk([choice({}, reason)])]
            stream = convert_stream(chunks)
            self.assertEqual(next(stream), chunks[0].model_dump(mode="json", exclude_unset=True))
            self.assertEqual(next(stream), chunks[1].model_dump(mode="json", exclude_unset=True))
            with self.assertRaises(ValueError):
                next(stream)
        for choices in ([], [choice({"role": "assistant", "content": "a"}, "stop", 0),
                             choice({"role": "assistant", "content": "b"}, "stop", 1)]):
            stream = convert_stream([chunk(choices)])
            next(stream)
            with self.assertRaises(ValueError):
                next(stream)


if __name__ == "__main__":
    unittest.main()
