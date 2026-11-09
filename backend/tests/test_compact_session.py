import ast
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from src.agent.compact import Summary, estimate_tokens, size, split_recent
from src.agent import session as sessions
from src.agent.session import create_session, open_session
from src.agent.tools.registry import create_registry
from src.agent.tools.todo import TodoList, snapshot
from src.agent.types import AssistantMessage, SystemMessage, ToolCallContent, ToolResultMessage, UserMessage
from src.ai.models.openai import to_openai_messages


def tool_data(name, arguments, call_id='call'):
    return {'turn_index': 1, 'tool_call_id': call_id, 'name': name, 'arguments': arguments}


def test_session_class_has_no_explicit_methods():
    tree = ast.parse(Path(sessions.__file__).read_text(encoding='utf-8'))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'Session')
    assert not any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) for node in cls.body)


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
    sessions.append_message(session, SystemMessage(content='system'), None)
    sessions.record_event(session, 'run', 'run.started', {'input': 'hello'})
    sessions.append_message(session, UserMessage(content='hello'), 'run')
    messages = to_openai_messages(sessions.context(session, 'run'))
    tools = [{'type': 'function', 'function': {'name': 'example'}}]
    assert sessions.context_tokens(session, 'run', 'model', messages, tools) == estimate_tokens(messages, tools)
    assert estimate_tokens(messages, tools) > estimate_tokens(messages[1:], [])


def test_summary_validation_and_compaction_coverage(tmp_path):
    session = create_session(tmp_path, secrets=())
    sessions.record_event(session, 'run1', 'run.started', {'input': 'old'})
    old = sessions.append_message(session, UserMessage(content='old'), 'run1')
    sessions.record_event(session, 'run1', 'run.completed', {'answer': ''})
    sessions.record_event(session, 'run2', 'run.started', {'input': 'new'})
    new = sessions.append_message(session, UserMessage(content='new'), 'run2')
    sessions.record_event(session, 'run2', 'compact.request', {'model': 'm', 'messages': [], 'tools': []})
    summary = dict(current_goal='goal', user_preferences=[], task_progress='', completed_tasks=[], next_tasks=[], decisions=[], relevant_facts=[], open_questions=[])
    with pytest.raises(ValidationError):
        Summary.model_validate({**summary, 'next_tasks': 'not a list'})
    with pytest.raises(ValueError, match='覆盖范围'):
        sessions.record_event(session, 'run2', 'compact.completed', {'summary': summary, 'covered': [new.id], 'previous': None})
    sessions.record_event(session, 'run2', 'compact.completed', {'summary': summary, 'covered': [old.id], 'previous': None})
    context = sessions.context(open_session(tmp_path, session.id, secrets=()), 'run2')
    assert context[0].role == 'system' and 'goal' in context[0].content
    assert context[1] == UserMessage(content='new')


def test_request_limit_is_persisted(tmp_path):
    session = create_session(tmp_path, secrets=())
    sessions.record_event(session, 'run', 'run.started', {'input': 'x'})
    for turn in range(1, 65):
        sessions.record_event(session, 'run', 'model.request', {'turn_index': turn, 'model': 'm', 'messages': [], 'tools': []})
    with pytest.raises(ValueError, match='上限'):
        sessions.record_event(session, 'run', 'model.request', {'turn_index': 65, 'model': 'm', 'messages': [], 'tools': []})
    assert len(sessions.trace(session, 'run')) == 65


def test_shared_registry_cannot_redirect_session_todos(tmp_path):
    first = create_session(tmp_path, secrets=())
    second = create_session(tmp_path, secrets=())
    unrelated = TodoList()
    registry = create_registry(unrelated)
    for session, title in [(first, 'A'), (second, 'B')]:
        arguments = json.dumps({'action': 'add', 'title': title})
        sessions.record_event(session, 'run', 'run.started', {'input': title})
        sessions.record_event(session, 'run', 'tool.started', tool_data('todo', arguments))
        sessions.process_tool_call(session, registry, 'run', 1, 'call', 'todo', arguments)
    assert sessions.todo_snapshot(first) == [{'id': 1, 'title': 'A', 'status': 'pending'}]
    assert sessions.todo_snapshot(second) == [{'id': 1, 'title': 'B', 'status': 'pending'}]
    assert snapshot(unrelated) == []


@pytest.mark.parametrize('name,arguments', [
    ('calculate', '{"left":"1","operator":"+","right":"2"}'),
    ('todo', '{"action":"add","title":"A"}'),
])
def test_tools_share_lifecycle_checks(tmp_path, name, arguments):
    session = create_session(tmp_path, secrets=())
    registry = create_registry()
    sessions.record_event(session, 'run', 'run.started', {'input': 'x'})
    with pytest.raises(ValueError, match='尚未启动'):
        sessions.process_tool_call(session, registry, 'run', 1, 'call', name, arguments)
    sessions.record_event(session, 'run', 'tool.started', tool_data(name, arguments))
    result = sessions.process_tool_call(session, registry, 'run', 1, 'call', name, arguments)
    with pytest.raises(ValueError, match='已经完成'):
        sessions.process_tool_call(session, registry, 'run', 1, 'call', name, arguments)
    assert sum(e.type == 'tool.completed' for e in sessions.trace(session, 'run')) == 1
    sessions.append_message(session, ToolResultMessage(tool_call_id='call', content=result), 'run')
    sessions.record_event(session, 'run', 'run.completed', {'answer': result})
    with pytest.raises(ValueError, match='已经结束'):
        sessions.process_tool_call(session, registry, 'run', 1, 'call', name, arguments)


@pytest.mark.parametrize('name,arguments', [
    ('calculate', '{"left":"1","operator":"+","right":"2"}'),
    ('todo', '{"action":"add","title":"A"}'),
])
def test_stopped_tool_does_not_commit(tmp_path, name, arguments):
    session = create_session(tmp_path, secrets=())
    sessions.record_event(session, 'run', 'run.started', {'input': 'x'})
    sessions.record_event(session, 'run', 'tool.started', tool_data(name, arguments))
    assert sessions.process_tool_call(session, create_registry(), 'run', 1, 'call', name, arguments, start=lambda: False) is None
    assert not any(e.type == 'tool.completed' for e in sessions.trace(session, 'run'))
    assert sessions.todo_snapshot(session) == []
    assert not session.todo_path.exists()


def test_todo_delete_requires_confirmation(tmp_path):
    session = create_session(tmp_path, secrets=())
    registry = create_registry()
    sessions.record_event(session, 'run', 'run.started', {'input': 'x'})
    add = '{"action":"add","title":"A"}'
    sessions.record_event(session, 'run', 'tool.started', tool_data('todo', add, 'add'))
    sessions.process_tool_call(session, registry, 'run', 1, 'add', 'todo', add)
    delete = '{"action":"delete","id":1}'
    sessions.record_event(session, 'run', 'tool.started', tool_data('todo', delete, 'delete'))
    with pytest.raises(ValueError, match='尚未确认'):
        sessions.process_tool_call(session, registry, 'run', 1, 'delete', 'todo', delete)
    assert sessions.todo_snapshot(session) == [{'id': 1, 'title': 'A', 'status': 'pending'}]
    sessions.record_event(session, 'run', 'tool.confirmation_requested', {
        'turn_index': 1, 'tool_call_id': 'delete', 'name': 'todo', 'action': 'delete', 'task': {'id': 1, 'title': 'A'},
    })
    sessions.record_event(session, 'run', 'tool.confirmation_resolved', {
        'turn_index': 1, 'tool_call_id': 'delete', 'decision': 'approved',
    })
    sessions.process_tool_call(session, registry, 'run', 1, 'delete', 'todo', delete)
    assert sessions.todo_snapshot(open_session(tmp_path, session.id, secrets=())) == []


def test_write_failure_poison_is_shared_by_reopened_session(tmp_path, monkeypatch):
    session = create_session(tmp_path, secrets=())
    reopened = open_session(tmp_path, session.id, secrets=())

    def fail_fsync(_):
        raise OSError('fsync failed')

    monkeypatch.setattr(sessions.os, 'fsync', fail_fsync)
    try:
        with pytest.raises(OSError, match='fsync failed'):
            sessions.record_event(session, 'run', 'run.started', {'input': 'x'})
        with pytest.raises(OSError, match='写入状态不确定'):
            sessions.read(reopened)
    finally:
        sessions._WRITE_FAILURES.discard(str(session.path))


def test_run_lock_can_be_released_from_another_thread(tmp_path):
    session = create_session(tmp_path, secrets=())
    reopened = open_session(tmp_path, session.id, secrets=())
    assert session.live_lock is reopened.live_lock
    session.run_lock.acquire()
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(sessions.release_run, session, 'run').result(timeout=5)
    assert not session.run_lock.is_locked
    with reopened.run_lock.acquire(blocking=False):
        pass


def test_interrupted_session_recovery_preserves_isolation(tmp_path):
    first = create_session(tmp_path, secrets=())
    second = create_session(tmp_path, secrets=())
    sessions.record_event(first, 'run', 'run.started', {'input': 'hello'})
    sessions.append_message(first, UserMessage(content='hello'), 'run')
    reopened = open_session(tmp_path, first.id, secrets=())
    assert sessions.recover(reopened)[0].data['status'] == 'interrupted'
    assert sessions.recover(reopened) == []
    assert [r.message.content for r in sessions.read(reopened) if hasattr(r, 'message')] == ['hello']
    assert not [r for r in sessions.read(second) if hasattr(r, 'message')]
