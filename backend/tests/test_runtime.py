import asyncio
import json
from decimal import DivisionByZero

import pytest
from openai import AsyncOpenAI

from src.agent import session as sessions
from src.agent.compact import CONTEXT_TOKENS, OUTPUT_TOKENS, estimate_tokens, size
from src.agent.runtime import append_message, close, confirm, create_agent, events, trace, wait
from src.agent.session import create_session, open_session
from src.agent.types import AssistantMessage, TextContent, UserMessage


def text_response(text):
    return [({'role': 'assistant', 'content': text}, None), ({}, 'stop')]


def tool_response(name, arguments, call_id='call1'):
    return [({'role': 'assistant', 'tool_calls': [{'index': 0, 'id': call_id, 'type': 'function',
               'function': {'name': name, 'arguments': json.dumps(arguments, ensure_ascii=False)}}]}, None), ({}, 'tool_calls')]


async def test_real_loop_tool_and_conversation_history(model_server, tmp_path):
    url, requests, responses = model_server
    responses.extend([tool_response('calculate', {'left': '2', 'operator': '+', 'right': '3'}), text_response('5'), text_response('好的')])
    session = create_session(tmp_path, secrets=())
    async with AsyncOpenAI(api_key='test-secret', base_url=url, max_retries=0) as client:
        agent = await create_agent(session, client, 'fixture')
        run_id = await append_message(agent, '2+3?')
        await wait(agent, run_id, timeout=10)
        events = await trace(agent, run_id)
        assert events[-1].type == 'run.completed' and events[-1].data['answer'] == '5'
        assert [e.type for e in events if e.type.startswith('tool.')] == ['tool.started', 'tool.completed']
        assert [e.data['tool_call_id'] for e in events if e.type.startswith('tool.')] == ['call1', 'call1']
        assert json.loads(next(e.data['result'] for e in events if e.type == 'tool.completed')) == {'result': '5'}
        assert len(requests) == 2
        assert all(request['max_tokens'] == 4000 for request in requests)
        assert requests[1]['messages'][-1] == {'role': 'tool', 'tool_call_id': 'call1', 'content': '{"result": "5"}'}
        assert requests[1]['messages'][-2]['tool_calls'][0]['id'] == 'call1'
        assert any(t['function']['name'] == 'todo' for t in requests[0]['tools'])
        followup = await append_message(agent, '谢谢')
        await wait(agent, followup, timeout=10)
        assert (await trace(agent, followup))[-1].data['answer'] == '好的'
        assert requests[2]['messages'][-1] == {'role': 'user', 'content': '谢谢'}
        assert requests[2]['messages'][-2]['content'] == '5'
        await close(agent)
    reopened = open_session(tmp_path, session.id, secrets=())
    assert any(isinstance(r.message, AssistantMessage) and r.message.content == [TextContent(text='5')]
               for r in sessions.read(reopened) if hasattr(r, 'message'))
    assert len(requests) == 3


async def test_split_secret_in_model_stream_is_not_persisted(model_server, tmp_path):
    url, _, responses = model_server
    responses.append([({'role': 'assistant', 'content': 'test-'}, None),
                      ({'content': 'secret'}, None), ({}, 'stop')])
    session = create_session(tmp_path, secrets=())
    async with AsyncOpenAI(api_key='test-secret', base_url=url, max_retries=0) as client:
        agent = await create_agent(session, client, 'fixture')
        try:
            run_id = await append_message(agent, 'hello')
            with pytest.raises(ValueError, match='密钥'):
                await wait(agent, run_id, timeout=10)
            assert 'test-secret' not in session.path.read_text(encoding='utf-8')
            assert (await trace(agent, run_id))[-1].data['status'] == 'failed'
        finally:
            await close(agent)


@pytest.mark.parametrize('decision', ['approved', 'rejected'])
async def test_delete_confirmation_through_agent_loop(model_server, tmp_path, decision):
    url, _, responses = model_server
    responses.extend([
        tool_response('todo', {'action': 'add', 'title': 'A'}, 'add'), text_response('added'),
        tool_response('todo', {'action': 'delete', 'id': 1}, 'delete'),
    ])
    if decision == 'approved':
        responses.append(text_response('deleted'))
    session = create_session(tmp_path, secrets=())
    async with AsyncOpenAI(api_key='test-secret', base_url=url, max_retries=0) as client:
        agent = await create_agent(session, client, 'fixture')
        try:
            added = await append_message(agent, '添加 A')
            await wait(agent, added, timeout=10)
            deleted = await append_message(agent, '删除 A')
            async with asyncio.timeout(10):
                async for event in events(agent, deleted):
                    if event.type == 'tool.confirmation_requested':
                        assert event.data['task'] == {'id': 1, 'title': 'A'}
                        await confirm(agent, deleted, event.data['turn_index'], event.data['tool_call_id'], decision)
                    if event.type in {'run.completed', 'run.finished'}:
                        break
            await wait(agent, deleted, timeout=10)
            terminal = (await trace(agent, deleted))[-1]
            if decision == 'approved':
                assert terminal.type == 'run.completed'
                assert sessions.todo_snapshot(session) == []
            else:
                assert terminal.data['status'] == 'stopped'
                assert sessions.todo_snapshot(session) == [{'id': 1, 'title': 'A', 'status': 'pending'}]
            assert responses == []
        finally:
            await close(agent)


async def test_todo_isolation_and_reopen_without_replay(model_server, tmp_path):
    url, requests, responses = model_server
    responses.extend([tool_response('todo', {'action': 'add', 'title': 'A'}), text_response('done')])
    first = create_session(tmp_path, secrets=())
    second = create_session(tmp_path, secrets=())
    async with AsyncOpenAI(api_key='test-secret', base_url=url, max_retries=0) as client:
        agent = await create_agent(first, client, 'fixture')
        run_id = await append_message(agent, '添加 A')
        await wait(agent, run_id, timeout=10)
        assert (await trace(agent, run_id))[-1].type == 'run.completed'
        await close(agent)
    reopened = open_session(tmp_path, first.id, secrets=())
    assert sessions.todo_snapshot(reopened) == [{'id': 1, 'title': 'A', 'status': 'pending'}]
    assert sessions.todo_snapshot(second) == []
    sessions.read(reopened)
    sessions.recover(reopened)
    assert sessions.todo_snapshot(reopened) == [{'id': 1, 'title': 'A', 'status': 'pending'}]
    assert len(requests) == 2


@pytest.mark.parametrize('name,arguments,error', [
    ('missing', {}, ValueError),
    ('calculate', {'left': '1', 'operator': '/', 'right': '0'}, DivisionByZero),
])
async def test_loop_failure_terminal_and_trace(model_server, tmp_path, name, arguments, error):
    url, requests, responses = model_server
    responses.append(tool_response(name, arguments))
    session = create_session(tmp_path, secrets=())
    async with AsyncOpenAI(api_key='test-secret', base_url=url, max_retries=0) as client:
        agent = await create_agent(session, client, 'fixture')
        run_id = await append_message(agent, 'do it')
        with pytest.raises(error):
            await wait(agent, run_id, timeout=10)
        events = await trace(agent, run_id)
        assert events[-1].type == 'run.finished' and events[-1].data['status'] == 'failed'
        assert events[-1].data['error'] == error.__name__
        assert 'test-secret' not in session.path.read_text(encoding='utf-8')
        assert any(e.type == 'tool.started' for e in events)
        assert not any(e.type == 'tool.completed' for e in events)
        assert len(requests) == 1
        await close(agent)


async def test_context_budget_failure_before_request(model_server, tmp_path):
    url, requests, _ = model_server
    session = create_session(tmp_path, secrets=())
    async with AsyncOpenAI(api_key='test-secret', base_url=url, max_retries=0) as client:
        agent = await create_agent(session, client, 'fixture')
        run_id = await append_message(agent, 'x' * 80001)
        with pytest.raises(ValueError, match='输入上限'):
            await wait(agent, run_id, timeout=10)
        events = await trace(agent, run_id)
        assert events[-1].type == 'run.finished' and events[-1].data['status'] == 'failed'
        assert requests == []
        await close(agent)


async def test_runtime_compacts_history_with_non_stream_summary(model_server, tmp_path):
    url, requests, responses = model_server
    summary = dict(current_goal='目标', user_preferences=[], task_progress='', completed_tasks=[], next_tasks=[], decisions=[], relevant_facts=[], open_questions=[])
    responses.extend([
        {'id': 'summary', 'created': 1, 'model': 'fixture', 'object': 'chat.completion',
         'choices': [{'index': 0, 'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': json.dumps(summary, ensure_ascii=False)}}]},
        text_response('完成'),
    ])
    session = create_session(tmp_path, secrets=())
    sessions.record_event(session, 'old', 'run.started', {'input': 'old'})
    old_user = sessions.append_message(session, UserMessage(content='x' * 56000), 'old')
    old_answer = sessions.append_message(session, AssistantMessage(content=[TextContent(text='earlier')]), 'old')
    sessions.record_event(session, 'old', 'run.completed', {'answer': 'earlier'})
    async with AsyncOpenAI(api_key='test-secret', base_url=url, max_retries=0) as client:
        agent = await create_agent(session, client, 'fixture')
        run_id = await append_message(agent, 'y' * 17000)
        await wait(agent, run_id, timeout=20)
        events = await trace(agent, run_id)
        compact = next(e for e in events if e.type == 'compact.completed')
        assert compact.data['covered'] == [old_user.id, old_answer.id]
        assert compact.data['summary'] == summary
        assert events[-1].type == 'run.completed'
        assert requests[0]['stream'] is False and 'tools' not in requests[0]
        assert requests[0]['max_tokens'] == requests[1]['max_tokens'] == 4000
        assert requests[1]['stream'] is True
        assert requests[1]['messages'][0]['role'] == 'system'
        assert '目标' in requests[1]['messages'][0]['content']
        assert requests[1]['messages'][-1]['content'] == 'y' * 17000
        await close(agent)


async def test_compaction_continues_without_post_budget_check(model_server, tmp_path):
    url, requests, responses = model_server
    summary = dict(current_goal='goal', user_preferences=[], task_progress='', completed_tasks=[],
                   next_tasks=[], decisions=[], relevant_facts=[], open_questions=[])
    responses.extend([
        {'id': 'summary', 'created': 1, 'model': 'fixture', 'object': 'chat.completion',
         'choices': [{'index': 0, 'finish_reason': 'stop', 'message': {
             'role': 'assistant', 'content': json.dumps(summary)}}]},
        text_response('done'),
    ])
    session = create_session(tmp_path, secrets=())
    sessions.record_event(session, 'old', 'run.started', {'input': 'old'})
    sessions.append_message(session, UserMessage(content='old'), 'old')
    sessions.append_message(session, AssistantMessage(content=[TextContent(text='answer')]), 'old')
    sessions.record_event(session, 'old', 'run.completed', {'answer': 'answer'})
    async with AsyncOpenAI(api_key='test-secret', base_url=url, max_retries=0) as client:
        agent = await create_agent(session, client, 'fixture')
        try:
            run_id = await append_message(agent, 'x' * 81000)
            await wait(agent, run_id, timeout=20)
            assert (await trace(agent, run_id))[-1].type == 'run.completed'
            assert len(requests) == 2 and requests[1]['messages'][-1]['content'] == 'x' * 81000
        finally:
            await close(agent)


async def test_loop_stops_at_request_limit(model_server, tmp_path):
    url, requests, responses = model_server
    responses.extend(tool_response('calculate', {'left': '1', 'operator': '+', 'right': '1'}, f'call{step}')
                     for step in range(64))
    session = create_session(tmp_path, secrets=())
    async with AsyncOpenAI(api_key='test-secret', base_url=url, max_retries=0) as client:
        agent = await create_agent(session, client, 'fixture')
        try:
            run_id = await append_message(agent, '继续计算')
            await wait(agent, run_id, timeout=90)
            events = await trace(agent, run_id)
            assert len(requests) == 64
            assert responses == []
            assert sum(e.type == 'tool.completed' for e in events) == 64
            assert sum(e.type in {'run.completed', 'run.finished'} for e in events) == 1
            assert events[-1].type == 'run.finished'
            assert events[-1].data['status'] == 'limited'
        finally:
            await close(agent)


def with_usage(response, total):
    return response + [(None, {'prompt_tokens': total, 'completion_tokens': 0, 'total_tokens': total,
                               'prompt_tokens_details': {'cached_tokens': total}})]


@pytest.mark.parametrize('total,should_compact', [(15992, False), (15993, True)])
async def test_usage_changes_compaction_for_same_messages(model_server, tmp_path, total, should_compact):
    url, requests, responses = model_server
    summary = dict(current_goal='goal', user_preferences=[], task_progress='', completed_tasks=[],
                   next_tasks=[], decisions=[], relevant_facts=[], open_questions=[])
    responses.append(with_usage(text_response('first'), total))
    if should_compact:
        responses.append({'id': 'summary', 'created': 1, 'model': 'fixture', 'object': 'chat.completion',
                          'choices': [{'index': 0, 'finish_reason': 'stop', 'message': {
                              'role': 'assistant', 'content': json.dumps(summary)}}]})
    responses.append(text_response('second'))
    if should_compact:
        responses.append(text_response('third'))
    session = create_session(tmp_path, secrets=())
    sessions.record_event(session, 'old', 'run.started', {'input': 'old'})
    sessions.append_message(session, UserMessage(content='old' * 6000), 'old')
    sessions.append_message(session, AssistantMessage(content=[TextContent(text='old answer')]), 'old')
    sessions.record_event(session, 'old', 'run.completed', {'answer': 'old answer'})
    async with AsyncOpenAI(api_key='test-secret', base_url=url, max_retries=0) as client:
        agent = await create_agent(session, client, 'fixture')
        try:
            first = await append_message(agent, 'x' * 17000)
            await wait(agent, first, timeout=20)
            followup = await append_message(agent, 'next')
            await wait(agent, followup, timeout=20)
            events = await trace(agent, followup)
            assert any(e.type == 'compact.completed' for e in events) is should_compact
            assert events[-1].type == 'run.completed'
            assert len(requests) == (3 if should_compact else 2)
            if should_compact:
                third = await append_message(agent, 'again')
                await wait(agent, third, timeout=20)
                assert not any(e.type == 'compact.completed' for e in await trace(agent, third))
                assert len(requests) == 4
        finally:
            await close(agent)


@pytest.mark.parametrize('last_total', [None, 0, 100])
async def test_usage_baseline_and_new_tool_results(model_server, tmp_path, last_total):
    url, requests, responses = model_server
    responses.append(with_usage(tool_response('calculate', {'left': '2', 'operator': '+', 'right': '3'}), 17000))
    final = text_response('5')
    responses.append(with_usage(final, last_total) if last_total is not None else final)
    session = create_session(tmp_path, secrets=())
    async with AsyncOpenAI(api_key='test-secret', base_url=url, max_retries=0) as client:
        agent = await create_agent(session, client, 'fixture')
        try:
            run_id = await append_message(agent, 'calculate')
            await wait(agent, run_id, timeout=10)
            messages = requests[1]['messages'] + [{'role': 'assistant', 'content': '5'}]
            tools = requests[1]['tools']
            tokens = sessions.context_tokens(session, run_id, 'fixture', messages, tools)
            if last_total:
                assert tokens == last_total
            else:
                assert tokens == 17000 + (size(messages[2:]) - size([])) / 4
            assert sessions.context_tokens(session, run_id, 'different-model', messages, tools) == estimate_tokens(messages, tools)
            assert sessions.context_tokens(session, run_id, 'fixture', messages, []) == estimate_tokens(messages, [])
            sessions.record_event(session, 'next', 'run.started', {'input': 'next'})
            sessions.append_message(session, UserMessage(content='next'), 'next')
            new_messages = messages + [{'role': 'user', 'content': 'next'}]
            if last_total:
                expected = last_total + (size(new_messages[4:]) - size([])) / 4
            else:
                expected = 17000 + (size(new_messages[2:]) - size([])) / 4
            assert sessions.context_tokens(session, 'next', 'fixture', new_messages, tools) == expected
        finally:
            await close(agent)


@pytest.mark.parametrize('total', [None, 0])
async def test_missing_usage_estimates_complete_context(model_server, tmp_path, total):
    url, requests, responses = model_server
    response = text_response('ok')
    responses.append(with_usage(response, total) if total is not None else response)
    session = create_session(tmp_path, secrets=())
    async with AsyncOpenAI(api_key='test-secret', base_url=url, max_retries=0) as client:
        agent = await create_agent(session, client, 'fixture')
        try:
            run_id = await append_message(agent, 'hello')
            await wait(agent, run_id, timeout=10)
            messages = requests[0]['messages'] + [{'role': 'assistant', 'content': 'ok'}]
            tools = requests[0]['tools']
            assert sessions.context_tokens(session, run_id, 'fixture', messages, tools) == estimate_tokens(messages, tools)
        finally:
            await close(agent)


async def test_usage_boundary_and_failed_run_isolation(model_server, tmp_path):
    url, requests, responses = model_server
    responses.extend([with_usage(text_response('ok'), 16000),
                      with_usage(tool_response('missing', {}), 50000)])
    session = create_session(tmp_path, secrets=())
    async with AsyncOpenAI(api_key='test-secret', base_url=url, max_retries=0) as client:
        agent = await create_agent(session, client, 'fixture')
        try:
            first = await append_message(agent, 'hello')
            await wait(agent, first, timeout=10)
            messages = requests[0]['messages'] + [{'role': 'assistant', 'content': 'ok'}]
            tools = requests[0]['tools']
            assert sessions.context_tokens(session, first, 'fixture', messages, tools) + OUTPUT_TOKENS == CONTEXT_TOKENS
            second = await append_message(agent, 'next')
            with pytest.raises(ValueError):
                await wait(agent, second, timeout=10)
            sessions.record_event(session, 'third', 'run.started', {'input': 'after'})
            sessions.append_message(session, UserMessage(content='after'), 'third')
            new_messages = messages + [{'role': 'user', 'content': 'after'}]
            tokens = sessions.context_tokens(session, 'third', 'fixture', new_messages, tools)
            assert tokens > 16000 and tokens < 50000
        finally:
            await close(agent)


async def test_tool_followup_changes_only_current_session(model_server, tmp_path):
    url, requests, responses = model_server
    responses.extend([
        tool_response('todo', {'action': 'add', 'title': '明天交周报'}, 'add-a'), text_response('已记录'),
        tool_response('todo', {'action': 'add', 'title': '准备面试'}, 'add-b'), text_response('已记录'),
        tool_response('todo', {'action': 'update', 'id': 1, 'title': '后天交周报'}, 'update-a'), text_response('已修改'),
    ])
    first, second = (create_session(tmp_path, secrets=()) for _ in range(2))
    async with AsyncOpenAI(api_key='test-secret', base_url=url, max_retries=0) as client:
        first_agent = await create_agent(first, client, 'fixture')
        second_agent = await create_agent(second, client, 'fixture')
        try:
            for agent, message in [(first_agent, '记待办：明天交周报'),
                                   (second_agent, '记待办：准备面试'),
                                   (first_agent, '把刚才那条改为后天交周报')]:
                run_id = await append_message(agent, message)
                await wait(agent, run_id, timeout=10)
                assert (await trace(agent, run_id))[-1].type == 'run.completed'
            assert sessions.todo_snapshot(open_session(tmp_path, first.id, secrets=())) == [
                {'id': 1, 'title': '后天交周报', 'status': 'pending'},
            ]
            assert sessions.todo_snapshot(open_session(tmp_path, second.id, secrets=())) == [
                {'id': 1, 'title': '准备面试', 'status': 'pending'},
            ]
            assert len(requests) == 6
            assert '准备面试' not in json.dumps(requests[4]['messages'], ensure_ascii=False)
            assert '明天交周报' in json.dumps(requests[4]['messages'], ensure_ascii=False)
            assert '明天交周报' not in json.dumps(requests[2]['messages'], ensure_ascii=False)
        finally:
            await close(first_agent)
            await close(second_agent)
