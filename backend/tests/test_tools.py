import base64
import json
from decimal import DecimalException

import pytest
from pydantic import ValidationError

from src.agent.tools.registry import create_registry, declarations, execute, get, register
from src.agent.tools.todo import TodoList


@pytest.fixture
def tools():
    return create_registry(TodoList())


def call(tools, name, **arguments):
    return execute(tools, name, json.dumps(arguments, ensure_ascii=False))


def test_registry_schema_and_unknown(tools):
    entries = declarations(tools)
    assert {item['function']['name'] for item in entries} == {'calculate', 'search', 'weather', 'todo', 'read', 'write'}
    assert all(item['function']['description'] and item['function']['parameters']['type'] == 'object' for item in entries)
    assert set(get(tools, 'calculate').parameters.model_json_schema()['required']) == {'left', 'operator', 'right'}
    with pytest.raises(ValueError, match='未知工具'):
        get(tools, 'missing')
    with pytest.raises(ValueError, match='重复'):
        register(tools, get(tools, 'todo'))
    with pytest.raises(ValidationError):
        call(tools, 'calculate', left=1, operator='+', right='2')


@pytest.mark.parametrize(('operator', 'result'), [('+', '3.5'), ('-', '1.5'), ('*', '2.5'), ('/', '2.5')])
def test_calculator(tools, operator, result):
    assert json.loads(call(tools, 'calculate', left='2.5', operator=operator, right='1'))['result'] == result


def test_calculator_failures(tools):
    with pytest.raises((ValueError, DecimalException)):
        call(tools, 'calculate', left='NaN', operator='+', right='1')
    with pytest.raises(DecimalException):
        call(tools, 'calculate', left='1', operator='/', right='0')


def test_search_and_weather(tools):
    result = json.loads(call(tools, 'search', query='退货', limit=1))
    assert result['source'] == 'mock' and len(result['results']) == 1
    assert json.loads(call(tools, 'search', query='not-in-corpus'))['results'] == []
    with pytest.raises(ValueError):
        call(tools, 'search', query='  ')
    with pytest.raises(ValidationError):
        call(tools, 'search', query='退货', limit=11)
    result = json.loads(call(tools, 'weather', location=' 广州 '))
    assert result['location'] == '广州' and result['is_live'] is False
    with pytest.raises(ValueError):
        call(tools, 'weather', location='未知')


def test_todo_lifecycle_and_invalid_commands(tools):
    item = json.loads(call(tools, 'todo', action='add', title=' first '))['item']
    assert item == {'id': 1, 'title': 'first', 'status': 'pending'}
    assert json.loads(call(tools, 'todo', action='update', id=1, title='new'))['item']['title'] == 'new'
    assert json.loads(call(tools, 'todo', action='set_status', id=1, status='completed'))['item']['status'] == 'completed'
    assert len(json.loads(call(tools, 'todo', action='list'))['items']) == 1
    assert json.loads(call(tools, 'todo', action='delete', id=1))['deleted']['id'] == 1
    assert json.loads(call(tools, 'todo', action='list'))['items'] == []
    with pytest.raises(ValueError):
        call(tools, 'todo', action='delete', id=1)
    with pytest.raises(ValueError):
        call(tools, 'todo', action='add', title='  ')


def test_file_tools(tools, tmp_path):
    path = str(tmp_path / 'file')
    assert call(tools, 'write', path=path, content='你好', mode='overwrite') == '6'
    call(tools, 'write', path=path, content='!', mode='append')
    assert call(tools, 'read', path=path) == '你好!'
    binary = base64.b64encode(b'\x00\xff').decode()
    call(tools, 'write', path=path, content=binary, mode='overwrite', encoding='base64')
    assert call(tools, 'read', path=path, encoding='base64') == binary
    with pytest.raises(ValueError):
        call(tools, 'write', path=path, content='@@@', mode='overwrite', encoding='base64')
    assert call(tools, 'read', path=path, encoding='base64') == binary
    with pytest.raises(FileNotFoundError):
        call(tools, 'read', path=str(tmp_path / 'missing'))
