from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from .calculator import Calculation, calculate
from .search import SearchQuery, search
from .todo import TodoCommand, TodoList
from .weather import WeatherQuery, weather


@dataclass(frozen=True)
class ToolDefinition[Parameters: BaseModel]:
    name: str
    description: str
    parameters: type[Parameters]
    execute: Callable[[Parameters], str]


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition[Any]] = {}

    def register(self, tool: ToolDefinition[Any]) -> None:
        if tool.name in self._tools:
            raise ValueError(f"工具名称重复: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> ToolDefinition[Any]:
        if name not in self._tools:
            raise ValueError(f"未知工具: {name}")
        return self._tools[name]

    def declarations(self) -> list[dict]:
        return [
            {"type": "function", "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters.model_json_schema(),
            }}
            for tool in self._tools.values()
        ]

    def execute(self, name: str, arguments: str) -> str:
        tool = self.get(name)
        parameters = tool.parameters.model_validate_json(arguments)
        result = tool.execute(parameters)
        if not isinstance(result, str):
            raise TypeError(f"工具返回值必须是字符串: {name}")
        return result


def create_loop_registry() -> ToolRegistry:
    tools = ToolRegistry()
    tools.register(ToolDefinition(
        name="calculate",
        description="计算两个数字的加、减、乘或除。",
        parameters=Calculation,
        execute=calculate,
    ))
    tools.register(ToolDefinition(
        name="weather",
        description="查询自定义样例天气数据，结果非实时。",
        parameters=WeatherQuery,
        execute=weather,
    ))
    tools.register(ToolDefinition(
        name="search",
        description="搜索自定义样例数据，结果非实时，不联网。",
        parameters=SearchQuery,
        execute=search,
    ))
    return tools


def create_full_registry(todo_list: TodoList) -> ToolRegistry:
    tools = create_loop_registry()
    tools.register(ToolDefinition(
        name="todo",
        description="添加、查看、修改状态或删除待办任务。",
        parameters=TodoCommand,
        execute=todo_list.execute,
    ))
    return tools


registry = create_loop_registry()
