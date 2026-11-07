import sys
from collections import deque
from collections.abc import AsyncIterable, AsyncIterator, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from openai.types.chat import ChatCompletionChunk
from pydantic import JsonValue, TypeAdapter

from ...agent.security import SecretGuard, StreamSecretGuard, check, contains, create_guard, feed, finish
from .openai import ModelResponse, async_convert_stream


@dataclass
class _NormalizedState:
    guard: SecretGuard
    stream_guard: StreamSecretGuard
    received: bool = False
    reason: str | None = None
    inconsistent_reason: bool = False
    usage: Any = None
    refusals: deque[str] = field(default_factory=deque)
    buffered: list[dict[str, str]] = field(default_factory=list)
    buffering: bool = False


def _observe(state: _NormalizedState, chunk: ChatCompletionChunk) -> None:
    state.received = True
    if chunk.usage is not None:
        state.usage = chunk.usage
    for choice in chunk.choices:
        if choice.index == 0 and choice.finish_reason is not None:
            if state.reason is not None and state.reason != choice.finish_reason:
                state.inconsistent_reason = True
            state.reason = choice.finish_reason


def _usage_data(state: _NormalizedState) -> dict[str, Any] | None:
    if state.usage is None:
        return None
    result = {name: getattr(state.usage, name) for name in ("prompt_tokens", "completion_tokens", "total_tokens")}
    for name in ("completion_tokens_details", "prompt_tokens_details"):
        details = getattr(state.usage, name)
        if details is not None:
            result[name] = details.model_dump(include=set(type(details).model_fields), exclude_none=True, warnings=False)
    return result


def _normalized_item(state: _NormalizedState, item: dict[str, Any] | ModelResponse) -> Iterator[dict[str, Any]]:
    if isinstance(item, ModelResponse):
        data = {"message": item.message.model_dump(mode="json"), "finish_reason": item.finish_reason,
                "usage": _usage_data(state)}
        check(state.guard, data)
        check(state.guard, "".join(block["text"] for block in data["message"]["content"] if block["type"] == "text"))
        for block in data["message"]["content"]:
            if block["type"] == "toolCall":
                check(state.guard, TypeAdapter(JsonValue).validate_json(block["arguments"]))
        for fragments in state.buffered:
            yield {"type": "model.delta", "data": {"fragments": fragments}}
        state.buffered.clear()
        state.buffering = False
        while state.refusals:
            for safe in feed(state.stream_guard, {"text": state.refusals.popleft()}):
                yield {"type": "model.delta", "data": {"fragments": safe}}
        for fragments in finish(state.stream_guard):
            yield {"type": "model.delta", "data": {"fragments": fragments}}
        yield {"type": "model.response", "data": data}
        return
    fragments: list[tuple[str, str]] = []
    for choice in item["choices"]:
        if choice["index"] != 0:
            raise ValueError("模型响应必须包含一个 choice")
        delta = choice["delta"]
        if delta.get("content") is not None:
            fragments.append(("text", delta["content"]))
        if delta.get("refusal") is not None:
            state.refusals.append(delta["refusal"])
        for call in delta.get("tool_calls") or []:
            prefix = f"tool:{call['index']}"
            if call.get("id") is not None:
                fragments.append((f"{prefix}:id", call["id"]))
            function = call.get("function") or {}
            for name in ("name", "arguments"):
                if function.get(name) is not None:
                    fragments.append((f"{prefix}:{name}", function[name]))
    for key, value in fragments:
        if key.startswith("tool:") and key.endswith(":arguments"):
            state.buffering = True
        for safe in feed(state.stream_guard, {key: value}):
            if state.buffering:
                state.buffered.append(safe)
            else:
                yield {"type": "model.delta", "data": {"fragments": safe}}


def _failure_events(state: _NormalizedState) -> Iterator[dict[str, Any]]:
    while state.refusals and not state.stream_guard.finished:
        refusal = state.refusals.popleft()
        pending_text = "".join(fragment.get("text", "") for fragment in state.stream_guard.pending)
        if contains(state.guard, {"text": pending_text + refusal}):
            break
        for safe in feed(state.stream_guard, {"text": refusal}):
            if not state.buffering or all(key == "text" for key in safe):
                yield {"type": "model.delta", "data": {"fragments": safe}}
    data = {"message": None, "finish_reason": None if state.inconsistent_reason else state.reason,
            "usage": _usage_data(state)}
    if not contains(state.guard, data):
        yield {"type": "model.response", "data": data}


async def async_normalized_stream(chunks: AsyncIterable[ChatCompletionChunk], secrets: Sequence[str]) -> AsyncIterator[dict[str, Any]]:
    guard = create_guard(secrets)
    state = _NormalizedState(guard, StreamSecretGuard(guard))

    async def observed_chunks() -> AsyncIterator[ChatCompletionChunk]:
        async for chunk in chunks:
            _observe(state, chunk)
            yield chunk

    try:
        async for item in async_convert_stream(observed_chunks()):
            for event in _normalized_item(state, item):
                yield event
    finally:
        if isinstance(sys.exception(), Exception) and state.received:
            for event in _failure_events(state):
                yield event
