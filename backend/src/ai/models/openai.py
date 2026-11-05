from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any

from openai import OpenAI, omit
from openai.lib.streaming.chat import ChatCompletionStreamState
from openai.types.chat import ChatCompletion, ChatCompletionChunk, ChatCompletionToolUnionParam
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


def to_openai_messages(messages: Sequence[AgentMessage]) -> list[ChatCompletionMessageParam]:
    result: list[ChatCompletionMessageParam] = []
    pending: set[str] = set()
    for message in messages:
        if isinstance(message, CustomMessage):
            continue
        if isinstance(message, ToolResultMessage):
            if message.tool_call_id not in pending:
                raise ValueError("工具结果没有匹配的调用 ID")
            pending.remove(message.tool_call_id)
            result.append({"role": "tool", "tool_call_id": message.tool_call_id, "content": message.content})
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
                    calls.append({"id": block.id, "type": "function", "function": {"name": block.name, "arguments": block.arguments}})
                else:
                    raise ValueError("未知内容块")
            converted: Any = {"role": "assistant", "content": "".join(texts) if texts or not calls else None}
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
        content.append(ToolCallContent(id=call.id, name=call.function.name, arguments=call.function.arguments))
    return AssistantMessage(content=content)


@dataclass(frozen=True)
class ModelResponse:
    response: ChatCompletion
    message: AssistantMessage
    finish_reason: str


def convert_stream(chunks: Iterable[ChatCompletionChunk]) -> Iterator[dict[str, Any] | ModelResponse]:
    """先逐个交付原始 chunk，结束后交付 ModelResponse。"""
    state: ChatCompletionStreamState[Any] = ChatCompletionStreamState()
    finished: dict[int, str] = {}
    usage = None
    for chunk in chunks:
        yield chunk.model_dump(mode="json", by_alias=True, exclude_unset=True, warnings=False)
        for choice in chunk.choices:
            if choice.index in finished:
                if (choice.delta.model_dump(exclude_unset=True, exclude_none=True, warnings=False)
                        or choice.logprobs is not None
                        or choice.finish_reason not in (None, finished[choice.index])):
                    raise ValueError("模型结束后出现内容或矛盾的结束原因")
            elif choice.finish_reason is not None:
                finished[choice.index] = choice.finish_reason
            if choice.delta.role not in (None, "assistant"):
                raise ValueError("不支持的模型消息角色")
            if choice.delta.function_call is not None:
                raise ValueError("不支持的工具调用类型")
            for call in choice.delta.tool_calls or []:
                if call.type not in (None, "function"):
                    raise ValueError("不支持的工具调用类型")
        if chunk.usage is not None:
            usage = chunk.usage
        state.handle_chunk(chunk)
    snapshot = state.current_completion_snapshot
    snapshot.usage = usage
    message = from_openai_response(snapshot)
    response = state.get_final_completion()
    yield ModelResponse(response=response, message=message, finish_reason=response.choices[0].finish_reason)


def stream_response(
    client: OpenAI,
    model: str,
    messages: Sequence[AgentMessage],
    tools: Iterable[ChatCompletionToolUnionParam] | None = None,
) -> Iterator[dict[str, Any] | ModelResponse]:
    """调用 OpenAI 流接口，按 convert_stream 的顺序交付结果。"""
    tool_list = list(tools) if tools is not None else None
    if tool_list is not None and any(tool["type"] != "function" for tool in tool_list):
        raise ValueError("不支持的工具类型")
    with client.chat.completions.create(
        model=model,
        messages=to_openai_messages(messages),
        tools=tool_list if tool_list is not None else omit,
        n=1,
        stream=True,
        stream_options={"include_usage": True},
    ) as stream:
        yield from convert_stream(stream)
