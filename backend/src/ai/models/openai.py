from http.cookiejar import DefaultCookiePolicy

import httpcore2
import httpx2
from openai import AsyncOpenAI

from collections.abc import AsyncIterable, AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from openai.lib.streaming.chat import ChatCompletionStreamState
from openai.types.chat import (
    ChatCompletion,
    ChatCompletionChunk,
)
from openai.types.chat.chat_completion_message_param import ChatCompletionMessageParam

from ...agent.types import (
    AgentMessage,
    AssistantMessage,
    CustomMessage,
    SystemMessage,
    TextContent,
    ToolCallContent,
    ToolResultMessage,
    UserMessage,
)


def to_openai_messages(
    messages: Sequence[AgentMessage],
) -> list[ChatCompletionMessageParam]:
    result: list[ChatCompletionMessageParam] = []
    pending: set[str] = set()
    for message in messages:
        if isinstance(message, CustomMessage):
            continue
        if isinstance(message, ToolResultMessage):
            if message.tool_call_id not in pending:
                raise ValueError("工具结果没有匹配的调用 ID")
            pending.remove(message.tool_call_id)
            result.append({
                "role": "tool",
                "tool_call_id": message.tool_call_id,
                "content": message.content,
            })
            continue
        if pending:
            raise ValueError("工具调用缺少结果")
        if isinstance(message, (SystemMessage, UserMessage)):
            result.append({"role": message.role, "content": message.content})
        elif isinstance(message, AssistantMessage):
            texts: list[str] = []
            calls = []
            for block in message.content:
                if isinstance(block, TextContent):
                    texts.append(block.text)
                elif isinstance(block, ToolCallContent):
                    if not block.id or block.id in pending:
                        raise ValueError("工具调用 ID 为空或重复")
                    pending.add(block.id)
                    calls.append({
                        "id": block.id,
                        "type": "function",
                        "function": {"name": block.name, "arguments": block.arguments},
                    })
                else:
                    raise ValueError("未知内容块")
            converted: Any = {
                "role": "assistant",
                "content": "".join(texts) if texts or not calls else None,
            }
            if calls:
                converted["tool_calls"] = calls
            result.append(converted)
        else:
            raise ValueError("未知消息角色")
    if pending:
        raise ValueError("工具调用缺少结果")
    return result


def from_openai_response(response: ChatCompletion) -> AssistantMessage:
    if len(response.choices) != 1:
        raise ValueError("模型响应必须包含一个 choice")
    choice = response.choices[0]
    message = choice.message
    if message.role != "assistant" or message.function_call is not None:
        raise ValueError("不支持的模型消息角色或工具调用")
    calls = message.tool_calls or []
    if message.refusal is not None and calls:
        raise ValueError("拒绝响应不能包含工具调用")
    if choice.finish_reason == "stop":
        if calls or (message.content is None and message.refusal is None):
            raise ValueError("模型响应与结束原因不一致")
    elif choice.finish_reason == "tool_calls":
        if not calls:
            raise ValueError("模型响应与结束原因不一致")
    else:
        raise ValueError("模型响应未完成或结束原因不受支持")
    content: list[TextContent | ToolCallContent] = []
    if message.content is not None:
        content.append(TextContent(text=message.content))
    if message.refusal is not None:
        content.append(TextContent(text=message.refusal))
    seen: set[str] = set()
    for call in calls:
        if call.type != "function":
            raise ValueError("不支持的工具调用类型")
        if not call.id or call.id in seen:
            raise ValueError("工具调用 ID 为空或重复")
        seen.add(call.id)
        content.append(
            ToolCallContent(
                id=call.id, name=call.function.name, arguments=call.function.arguments
            )
        )
    return AssistantMessage(content=content)


@dataclass(frozen=True)
class ModelResponse:
    response: ChatCompletion
    message: AssistantMessage
    finish_reason: str


@dataclass
class _StreamState:
    completion: ChatCompletionStreamState[Any] = field(
        default_factory=ChatCompletionStreamState
    )
    finished: dict[int, str] = field(default_factory=dict)
    usage: Any = None


def _accumulate_chunk(state: _StreamState, chunk: ChatCompletionChunk) -> None:
    for choice in chunk.choices:
        if choice.index in state.finished:
            if (
                choice.delta.model_dump(
                    exclude_unset=True, exclude_none=True, warnings=False
                )
                or choice.logprobs is not None
                or choice.finish_reason not in (None, state.finished[choice.index])
            ):
                raise ValueError("模型结束后出现内容或矛盾的结束原因")
        elif choice.finish_reason is not None:
            state.finished[choice.index] = choice.finish_reason
        if choice.delta.role not in (None, "assistant"):
            raise ValueError("不支持的模型消息角色")
        if choice.delta.function_call is not None:
            raise ValueError("不支持的工具调用类型")
        for call in choice.delta.tool_calls or []:
            if call.type not in (None, "function"):
                raise ValueError("不支持的工具调用类型")
    if chunk.usage is not None:
        state.usage = chunk.usage
    state.completion.handle_chunk(chunk)


def _complete_stream(state: _StreamState) -> ModelResponse:
    snapshot = state.completion.current_completion_snapshot
    snapshot.usage = state.usage
    message = from_openai_response(snapshot)
    response = state.completion.get_final_completion()
    return ModelResponse(
        response=response,
        message=message,
        finish_reason=response.choices[0].finish_reason,
    )


async def async_convert_stream(
    chunks: AsyncIterable[ChatCompletionChunk],
) -> AsyncIterator[dict[str, Any] | ModelResponse]:
    state = _StreamState()
    async for chunk in chunks:
        yield chunk.model_dump(
            mode="json", by_alias=True, exclude_unset=True, warnings=False
        )
        _accumulate_chunk(state, chunk)
    yield _complete_stream(state)


class reject_cookies(DefaultCookiePolicy):
    def set_ok(self, cookie, request) -> bool:
        return False

    def return_ok(self, cookie, request) -> bool:
        return False


def client_credentials(client: AsyncOpenAI) -> tuple[str, ...]:
    values = (client.api_key, client.admin_api_key, client.webhook_secret)
    if (
        not isinstance(client.api_key, str)
        or not client.api_key
        or any(value is not None and not isinstance(value, str) for value in values)
    ):
        raise ValueError("模型客户端必须使用固定 API key")
    return tuple(value for value in values if value)


def validate_client(client: AsyncOpenAI) -> tuple[str, ...]:
    credentials = client_credentials(client)
    transports = (client._client._transport, *client._client._mounts.values())
    if (
        client._api_key_provider is not None
        or client._provider_runtime is not None
        or client.workload_identity is not None
        or client._client.auth is not None
        or client.base_url.userinfo
        or client._client.base_url.userinfo
        or client.base_url.query
        or client._client.base_url.query
        or client.default_query
        or client._client.params
        or client._client.cookies
        or any(client._client.event_hooks.values())
        or any(
            name.lower()
            in {
                "authorization",
                "proxy-authorization",
                "api-key",
                "x-api-key",
                "cookie",
            }
            for name in (*client.default_headers, *client._client.headers)
        )
    ):
        raise ValueError("模型客户端必须使用固定 API key")
    for transport in transports:
        if transport is None:
            continue
        if type(transport) is not httpx2.AsyncHTTPTransport:
            raise ValueError("不支持自定义 HTTP transport")
        pool = transport._pool
        if type(pool) not in {httpcore2.AsyncConnectionPool, httpcore2.AsyncHTTPProxy}:
            raise ValueError("不支持该代理类型")
        if type(pool) is httpcore2.AsyncHTTPProxy and pool._proxy_headers:
            raise ValueError("不支持代理认证")
    return credentials
