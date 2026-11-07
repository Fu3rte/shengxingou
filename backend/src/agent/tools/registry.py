from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from typing import Any

from pydantic import BaseModel, JsonValue, TypeAdapter

from ..security import check, create_guard
from .calculator import Calculation, calculate
from .search import SearchQuery, search
from .todo import TodoCommand, TodoList
from .todo import (
    execute as execute_todo,
    confirmation_request as todo_confirmation_request,
)
from .weather import WeatherQuery, weather


@dataclass(frozen=True)
class ToolDefinition[Parameters: BaseModel]:
    name: str
    description: str
    parameters: type[Parameters]
    execute: Callable[..., str | None]
    confirmation: Callable[..., dict | None] | None = None
    contextual: bool = False


type ToolRegistry = dict[str, ToolDefinition[Any]]


def register(tools: ToolRegistry, tool: ToolDefinition[Any]) -> None:
    if tool.name in tools:
        raise ValueError(f"工具名称重复: {tool.name}")
    tools[tool.name] = tool


def get(tools: ToolRegistry, name: str) -> ToolDefinition[Any]:
    if name not in tools:
        raise ValueError(f"未知工具: {name}")
    return tools[name]


def declarations(tools: ToolRegistry) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters.model_json_schema(),
            },
        }
        for tool in tools.values()
    ]


def execute(
    tools: ToolRegistry,
    name: str,
    arguments: str,
    *,
    secrets: tuple[str, ...] = (),
    start: Callable[[], bool] | None = None,
    context: tuple[str, int, str] | None = None,
) -> str | None:
    guard = create_guard(secrets)
    check(guard, {"name": name, "arguments": arguments})
    tool = get(tools, name)
    check(guard, TypeAdapter(JsonValue).validate_json(arguments))
    parameters = tool.parameters.model_validate_json(arguments)
    check(guard, parameters)
    if tool.contextual:
        if context is None:
            raise ValueError("工具执行需要运行上下文")
        result = tool.execute(*context, arguments, start=start)
    else:
        result = (
            tool.execute(parameters)
            if start is None
            else tool.execute(parameters, start=start)
        )
    if result is None and start is not None:
        return None
    if not isinstance(result, str):
        raise TypeError(f"工具返回值必须是字符串: {name}")
    return result


def create_loop_registry() -> ToolRegistry:
    tools: ToolRegistry = {}
    register(
        tools,
        ToolDefinition(
            name="calculate",
            description="计算两个数字的加、减、乘或除。",
            parameters=Calculation,
            execute=calculate,
        ),
    )
    register(
        tools,
        ToolDefinition(
            name="weather",
            description="查询自定义样例天气数据，结果非实时。",
            parameters=WeatherQuery,
            execute=weather,
        ),
    )
    register(
        tools,
        ToolDefinition(
            name="search",
            description="搜索自定义样例数据，结果非实时，不联网。",
            parameters=SearchQuery,
            execute=search,
        ),
    )
    return tools


def create_full_registry(
    todo_list: TodoList | None,
    todo_snapshot: Callable[[], list[dict]] | None = None,
    *,
    todo_execute: Callable[..., str | None] | None = None,
) -> ToolRegistry:
    if todo_list is None and todo_execute is None:
        raise ValueError("待办工具缺少执行器")
    tools = create_loop_registry()
    register(
        tools,
        ToolDefinition(
            name="todo",
            description="添加、查看、修改状态或删除待办任务。",
            parameters=TodoCommand,
            execute=todo_execute
            if todo_execute is not None
            else partial(execute_todo, todo_list),
            confirmation=(
                lambda command: todo_confirmation_request(command, todo_snapshot)
            )
            if todo_snapshot
            else None,
            contextual=todo_execute is not None,
        ),
    )
    return tools


registry = create_loop_registry()
