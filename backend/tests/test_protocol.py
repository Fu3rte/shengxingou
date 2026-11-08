import pytest
from openai.types.chat import ChatCompletion, ChatCompletionChunk

from src.agent.types import AssistantMessage, TextContent, ToolCallContent, ToolResultMessage, UserMessage
from src.ai.models.openai import from_openai_response, to_openai_messages
from src.ai.models.normalized import async_normalized_stream


def completion(message, reason):
    return ChatCompletion.model_validate({'id': 'c', 'created': 1, 'model': 'm', 'object': 'chat.completion',
                                          'choices': [{'index': 0, 'finish_reason': reason, 'message': message}]})


def test_non_stream_parsing_and_call_pairing():
    parsed = from_openai_response(completion({'role': 'assistant', 'content': None, 'tool_calls': [
        {'id': 'call1', 'type': 'function', 'function': {'name': 'calculate', 'arguments': '{}'}}]}, 'tool_calls'))
    assert parsed.content == [ToolCallContent(id='call1', name='calculate', arguments='{}')]
    converted = to_openai_messages([UserMessage(content='hi'), parsed, ToolResultMessage(tool_call_id='call1', content='ok')])
    assert converted[1]['tool_calls'][0]['id'] == converted[2]['tool_call_id'] == 'call1'
    with pytest.raises(ValueError, match='匹配'):
        to_openai_messages([parsed, ToolResultMessage(tool_call_id='other', content='x')])
    with pytest.raises(ValueError, match='缺少'):
        to_openai_messages([parsed])
    with pytest.raises(ValueError):
        from_openai_response(completion({'role': 'assistant', 'content': 'truncated'}, 'length'))
    assert from_openai_response(completion({'role': 'assistant', 'content': 'ok'}, 'stop')).content == [TextContent(text='ok')]


async def test_stream_parses_fragmented_call():
    deltas = [
        ({'role': 'assistant', 'tool_calls': [{'index': 0, 'id': 'call1', 'type': 'function', 'function': {'name': 'calculate', 'arguments': '{"left":'}}]}, None),
        ({'tool_calls': [{'index': 0, 'function': {'arguments': '"2","operator":"+","right":"3"}'}}]}, None),
        ({}, 'tool_calls'),
    ]
    async def chunks():
        for delta, reason in deltas:
            yield ChatCompletionChunk.model_validate({'id': 'c', 'created': 1, 'model': 'm', 'object': 'chat.completion.chunk', 'choices': [{'index': 0, 'delta': delta, 'finish_reason': reason}]})
    events = [event async for event in async_normalized_stream(chunks(), ())]
    assert any(event['type'] == 'model.delta' for event in events)
    response = events[-1]['data']
    assert response['finish_reason'] == 'tool_calls'
    assert response['message']['content'] == [{'type': 'toolCall', 'id': 'call1', 'name': 'calculate', 'arguments': '{"left":"2","operator":"+","right":"3"}'}]
