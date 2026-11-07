import sys
from collections import deque
from collections.abc import Iterable, Iterator, Sequence
from typing import Any

from openai.types.chat import ChatCompletionChunk
from pydantic import JsonValue, TypeAdapter

from ...agent.security import SecretGuard, StreamSecretGuard
from .openai import ModelResponse, convert_stream


def normalized_stream(chunks: Iterable[ChatCompletionChunk], secrets: Sequence[str]) -> Iterator[dict[str, Any]]:
    guard = SecretGuard(secrets)
    stream_guard = StreamSecretGuard(secrets)
    received = False
    reason: str | None = None
    inconsistent_reason = False
    usage = None
    refusals: deque[str] = deque()
    buffered: list[dict[str, str]] = []
    buffering = False

    def observed_chunks() -> Iterator[ChatCompletionChunk]:
        nonlocal received, reason, inconsistent_reason, usage
        for chunk in chunks:
            received = True
            if chunk.usage is not None:
                usage = chunk.usage
            for choice in chunk.choices:
                if choice.index == 0 and choice.finish_reason is not None:
                    if reason is not None and reason != choice.finish_reason:
                        inconsistent_reason = True
                    reason = choice.finish_reason
            yield chunk

    def usage_data() -> dict[str, Any] | None:
        if usage is None:
            return None
        result = {name: getattr(usage, name) for name in ("prompt_tokens", "completion_tokens", "total_tokens")}
        for name in ("completion_tokens_details", "prompt_tokens_details"):
            details = getattr(usage, name)
            if details is not None:
                result[name] = details.model_dump(include=set(type(details).model_fields), exclude_none=True, warnings=False)
        return result

    try:
        for item in convert_stream(observed_chunks()):
            if isinstance(item, ModelResponse):
                data = {"message": item.message.model_dump(mode="json"), "finish_reason": item.finish_reason,
                        "usage": usage_data()}
                guard.check(data)
                guard.check("".join(block["text"] for block in data["message"]["content"] if block["type"] == "text"))
                for block in data["message"]["content"]:
                    if block["type"] == "toolCall":
                        guard.check(TypeAdapter(JsonValue).validate_json(block["arguments"]))
                for fragments in buffered:
                    yield {"type": "model.delta", "data": {"fragments": fragments}}
                buffered.clear()
                buffering = False
                while refusals:
                    for safe in stream_guard.feed({"text": refusals.popleft()}):
                        yield {"type": "model.delta", "data": {"fragments": safe}}
                for fragments in stream_guard.finish():
                    yield {"type": "model.delta", "data": {"fragments": fragments}}
                yield {"type": "model.response", "data": data}
                continue
            fragments: list[tuple[str, str]] = []
            for choice in item["choices"]:
                if choice["index"] != 0:
                    raise ValueError("模型响应必须包含一个 choice")
                delta = choice["delta"]
                if delta.get("content") is not None:
                    fragments.append(("text", delta["content"]))
                if delta.get("refusal") is not None:
                    refusals.append(delta["refusal"])
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
                    buffering = True
                for safe in stream_guard.feed({key: value}):
                    if buffering:
                        buffered.append(safe)
                    else:
                        yield {"type": "model.delta", "data": {"fragments": safe}}
    finally:
        if isinstance(sys.exception(), Exception) and received:
            while refusals and not stream_guard.finished:
                refusal = refusals.popleft()
                pending_text = "".join(fragment.get("text", "") for fragment in stream_guard.pending)
                # 预检避免密钥异常覆盖原始流错误。
                if guard.contains({"text": pending_text + refusal}):
                    break
                for safe in stream_guard.feed({"text": refusal}):
                    if not buffering or all(key == "text" for key in safe):
                        yield {"type": "model.delta", "data": {"fragments": safe}}
            data = {"message": None, "finish_reason": None if inconsistent_reason else reason, "usage": usage_data()}
            if not guard.contains(data):
                yield {"type": "model.response", "data": data}
