from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from src.agent.compact import Summary, estimate_tokens, size, split_recent
from src.agent.session import create_session, open_session
from src.agent.types import AssistantMessage, SystemMessage, ToolCallContent, ToolResultMessage, UserMessage
from src.ai.models.openai import to_openai_messages


def test_split_recent_keeps_latest_user_and_whole_tool_group():
    records = [SimpleNamespace(message=message) for message in [
        UserMessage(content='old'),
        AssistantMessage(content=[ToolCallContent(id='c', name='calculate', arguments='{}')]),
        ToolResultMessage(tool_call_id='c', content='result'),
        UserMessage(content='latest' + 'x' * 17000),
    ]]
    assert split_recent(records) == records[:3]
    records[-1].message = UserMessage(content='latest')
    records[2].message = ToolResultMessage(tool_call_id='c', content='x' * 17000)
    assert split_recent(records) == records[:1]
    assert size({'text': '中'}) == len('{"text":"中"}')


def test_first_request_estimates_system_and_tools(tmp_path):
    session = create_session(tmp_path, secrets=())
    session.append_message(SystemMessage(content='system'), None)
    session.append_event('run', 'run.started', {'input': 'hello'})
    session.append_message(UserMessage(content='hello'), 'run')
    messages = to_openai_messages(session.context('run'))
    tools = [{'type': 'function', 'function': {'name': 'example'}}]
    assert session.context_tokens('run', 'model', messages, tools) == estimate_tokens(messages, tools)
    assert estimate_tokens(messages, tools) > estimate_tokens(messages[1:], [])


def test_summary_validation_and_compaction_coverage(tmp_path):
    session = create_session(tmp_path, secrets=())
    session.append_event('run1', 'run.started', {'input': 'old'})
    old = session.append_message(UserMessage(content='old'), 'run1')
    session.append_event('run1', 'run.completed', {'answer': ''})
    session.append_event('run2', 'run.started', {'input': 'new'})
    new = session.append_message(UserMessage(content='new'), 'run2')
    session.append_event('run2', 'compact.request', {'model': 'm', 'messages': [], 'tools': []})
    summary = dict(current_goal='goal', user_preferences=[], task_progress='', completed_tasks=[], next_tasks=[], decisions=[], relevant_facts=[], open_questions=[])
    with pytest.raises(ValidationError):
        Summary.model_validate({**summary, 'next_tasks': 'not a list'})
    with pytest.raises(ValueError, match='覆盖范围'):
        session.append_event('run2', 'compact.completed', {'summary': summary, 'covered': [new.id], 'previous': None})
    session.append_event('run2', 'compact.completed', {'summary': summary, 'covered': [old.id], 'previous': None})
    context = open_session(tmp_path, session.id, secrets=()).context('run2')
    assert context[0].role == 'system' and 'goal' in context[0].content
    assert context[1] == UserMessage(content='new')


def test_request_limit_is_persisted(tmp_path):
    session = create_session(tmp_path, secrets=())
    session.append_event('run', 'run.started', {'input': 'x'})
    for turn in range(1, 65):
        session.append_event('run', 'model.request', {'turn_index': turn, 'model': 'm', 'messages': [], 'tools': []})
    with pytest.raises(ValueError, match='上限'):
        session.append_event('run', 'model.request', {'turn_index': 65, 'model': 'm', 'messages': [], 'tools': []})
    assert len(session.trace('run')) == 65


def test_interrupted_session_recovery_preserves_isolation(tmp_path):
    first = create_session(tmp_path, secrets=())
    second = create_session(tmp_path, secrets=())
    first.append_event('run', 'run.started', {'input': 'hello'})
    first.append_message(UserMessage(content='hello'), 'run')
    reopened = open_session(tmp_path, first.id, secrets=())
    assert reopened.recover()[0].data['status'] == 'interrupted'
    assert reopened.recover() == []
    assert [r.message.content for r in reopened.read() if hasattr(r, 'message')] == ['hello']
    assert not [r for r in second.read() if hasattr(r, 'message')]
