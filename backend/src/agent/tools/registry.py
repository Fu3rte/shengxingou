from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from typing import Any

from pydantic import BaseModel, JsonValue, TypeAdapter

from ..security import check, create_guard
from .calculator import Calculation, calculate
from .files import ReadFile, WriteFile, decode_content, read, write
from .search import SearchQuery, search
from .todo import TodoCommand, TodoList, apply_command
from .weather import WeatherQuery, weather


@dataclass(frozen=True)
class ToolDefinition[Parameters: BaseModel]:
    name: str
    description: str
    parameters: type[Parameters]
    handler: Callable[..., str | None] | None = None


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


def parse_arguments(
    tools: ToolRegistry, name: str, arguments: str, *, secrets: tuple[str, ...] = ()
) -> BaseModel:
    guard = create_guard(secrets)
    check(guard, {"name": name, "arguments": arguments})
    tool = get(tools, name)
    check(guard, TypeAdapter(JsonValue).validate_json(arguments))
    parameters = tool.parameters.model_validate_json(arguments)
    check(guard, parameters)
    if isinstance(parameters, WriteFile):
        check(guard, decode_content(parameters.content, parameters.encoding))
    return parameters


def invoke_handler(
    tool: ToolDefinition,
    parameters: BaseModel,
    *,
    secrets: tuple[str, ...] = (),
    start: Callable[[], bool] | None = None,
) -> str | None:
    if tool.handler is None:
        raise ValueError("工具需要会话存储")
    result = (
        tool.handler(parameters)
        if start is None
        else tool.handler(parameters, start=start)
    )
    if result is None and start is not None:
        return None
    if not isinstance(result, str):
        raise TypeError(f"工具返回值必须是字符串: {tool.name}")
    guard = create_guard(secrets)
    check(guard, result)
    if isinstance(parameters, ReadFile):
        check(guard, decode_content(result, parameters.encoding))
    return result


def create_registry(todo_list: TodoList | None = None) -> ToolRegistry:
    tools: ToolRegistry = {}
    for tool in (
        ToolDefinition(
            name="calculate",
            description="计算两个数字的加、减、乘或除。",
            parameters=Calculation,
            handler=calculate,
        ),
        ToolDefinition(
            name="weather",
            description="查询自定义样例天气数据，结果非实时。",
            parameters=WeatherQuery,
            handler=weather,
        ),
        ToolDefinition(
            name="search",
            description="搜索自定义样例数据，结果非实时，不联网。",
            parameters=SearchQuery,
            handler=search,
        ),
        ToolDefinition(
            name="read",
            description="读取文件，支持 UTF-8 文本和 Base64 编码的二进制内容。",
            parameters=ReadFile,
            handler=read,
        ),
        ToolDefinition(
            name="write",
            description="覆盖或追加文件内容，支持 UTF-8 文本和 Base64 编码的二进制内容。",
            parameters=WriteFile,
            handler=write,
        ),
        ToolDefinition(
            name="todo",
            description="添加、查看、修改标题或状态、删除待办任务。",
            parameters=TodoCommand,
            handler=partial(apply_command, todo_list) if todo_list is not None else None,
        ),
    ):
        register(tools, tool)
    return tools
