from collections.abc import Iterable, Iterator, Sequence
from typing import Any

from openai.types.chat import ChatCompletionChunk

from ...agent.security import SecretGuard, StreamSecretGuard
from .openai import ModelResponse, convert_stream


def normalized_stream(chunks: Iterable[ChatCompletionChunk], secrets: Sequence[str]) -> Iterator[dict[str, Any]]:
    guard = SecretGuard(secrets)
    stream_guard = StreamSecretGuard(secrets)
    for item in convert_stream(chunks):
        if isinstance(item, ModelResponse):
            for fragments in stream_guard.finish():
                yield {"type": "model.delta", "data": {"fragments": fragments}}
            usage = item.response.usage
            data = {
                "message": item.message.model_dump(mode="json"),
                "finish_reason": item.finish_reason,
                "usage": None if usage is None else {
                    "prompt_tokens": usage.prompt_tokens,
                    "completion_tokens": usage.completion_tokens,
                    "total_tokens": usage.total_tokens,
                },
            }
            if usage is not None:
                for name in ("completion_tokens_details", "prompt_tokens_details"):
                    details = getattr(usage, name)
                    if details is not None:
                        data["usage"][name] = details.model_dump(
                            include=set(type(details).model_fields), exclude_none=True, warnings=False,
                        )
            guard.check(data)
            guard.check("".join(block["text"] for block in data["message"]["content"] if block["type"] == "text"))
            yield {"type": "model.response", "data": data}
            continue
        fragments: list[tuple[str, str]] = []
        for choice in item["choices"]:
            if choice["index"] != 0:
                raise ValueError("模型响应必须包含一个 choice")
            delta = choice["delta"]
            for name in ("content", "refusal"):
                if delta.get(name) is not None:
                    fragments.append(("text", delta[name]))
            for call in delta.get("tool_calls") or []:
                prefix = f"tool:{call['index']}"
                if call.get("id") is not None:
                    fragments.append((f"{prefix}:id", call["id"]))
                function = call.get("function") or {}
                for name in ("name", "arguments"):
                    if function.get(name) is not None:
                        fragments.append((f"{prefix}:{name}", function[name]))
        for key, value in fragments:
            for safe in stream_guard.feed({key: value}):
                yield {"type": "model.delta", "data": {"fragments": safe}}
