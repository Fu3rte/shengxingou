import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Literal
from uuid import uuid4

from filelock import FileLock
from pydantic import BaseModel, ConfigDict, Field

from .files import read_bytes, write_bytes

TodoStatus = Literal["pending", "in_progress", "completed"]
TITLE_MAX_LENGTH = 200


class TodoItem(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=TITLE_MAX_LENGTH)
    status: TodoStatus


class TodoCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    action: Literal["add", "list", "update", "set_status", "delete"]
    title: str | None = Field(
        default=None,
        min_length=1,
        max_length=TITLE_MAX_LENGTH,
        description="任务标题，add 与 update 时必填",
    )
    id: int | None = Field(
        default=None, ge=1, description="任务 ID，update、set_status 与 delete 时必填"
    )
    status: TodoStatus | None = Field(default=None, description="set_status 时必填")


@dataclass
class TodoList:
    lock: Lock = field(default_factory=Lock)
    items: dict[int, dict] = field(default_factory=dict)
    next_id: int = 1


def restore(items: list[dict], next_id: int) -> TodoList:
    validated = [TodoItem.model_validate(item).model_dump() for item in items]
    ids = {item["id"] for item in validated}
    if (
        len(ids) != len(validated)
        or type(next_id) is not int
        or next_id < 1
        or (ids and next_id <= max(ids))
    ):
        raise ValueError("无效待办快照")
    return TodoList(items={item["id"]: item for item in validated}, next_id=next_id)


def add(store: TodoList, title: str) -> dict:
    with store.lock:
        item = {"id": store.next_id, "title": title, "status": "pending"}
        store.items[store.next_id] = item
        store.next_id += 1
        return dict(item)


def snapshot(store: TodoList) -> list[dict]:
    with store.lock:
        return [dict(item) for item in store.items.values()]


def update(store: TodoList, item_id: int, title: str) -> dict:
    with store.lock:
        item = store.items.get(item_id)
        if item is None:
            raise ValueError(f"任务不存在: {item_id}")
        item["title"] = title
        return dict(item)


def set_status(store: TodoList, item_id: int, status: TodoStatus) -> dict:
    with store.lock:
        item = store.items.get(item_id)
        if item is None:
            raise ValueError(f"任务不存在: {item_id}")
        item["status"] = status
        return dict(item)


def delete(store: TodoList, item_id: int) -> dict:
    with store.lock:
        item = store.items.pop(item_id, None)
        if item is None:
            raise ValueError(f"任务不存在: {item_id}")
        return dict(item)


def apply_command(
    store: TodoList, command: TodoCommand, *, start: Callable[[], bool] | None = None
) -> str | None:
    if start is not None and not start():
        return None
    match command.action:
        case "add":
            if command.title is None:
                raise ValueError("add 需要 title")
            title = command.title.strip()
            if not title:
                raise ValueError("标题不能为空")
            return json.dumps({"item": add(store, title)}, ensure_ascii=False)
        case "list":
            return json.dumps({"items": snapshot(store)}, ensure_ascii=False)
        case "update":
            if command.id is None or command.title is None:
                raise ValueError("update 需要 id 与 title")
            title = command.title.strip()
            if not title:
                raise ValueError("标题不能为空")
            return json.dumps({"item": update(store, command.id, title)}, ensure_ascii=False)
        case "set_status":
            if command.id is None or command.status is None:
                raise ValueError("set_status 需要 id 与 status")
            return json.dumps(
                {"item": set_status(store, command.id, command.status)},
                ensure_ascii=False,
            )
        case "delete":
            if command.id is None:
                raise ValueError("delete 需要 id")
            return json.dumps(
                {"deleted": delete(store, command.id)}, ensure_ascii=False
            )


def validate_result(arguments: str, value: str) -> None:
    command = TodoCommand.model_validate_json(arguments)
    result = json.loads(value)
    key = (
        "items"
        if command.action == "list"
        else "deleted"
        if command.action == "delete"
        else "item"
    )
    if not isinstance(result, dict) or set(result) != {key}:
        raise ValueError("待办记录无效")
    if command.action == "list":
        if not isinstance(result[key], list):
            raise ValueError("待办记录无效")
        for item in result[key]:
            TodoItem.model_validate(item)
        return
    item = TodoItem.model_validate(result[key]).model_dump()
    if (
        command.action == "add"
        and (
            command.title is None
            or item["title"] != command.title.strip()
            or item["status"] != "pending"
        )
        or command.action == "update"
        and (
            command.id is None
            or command.title is None
            or item["id"] != command.id
            or item["title"] != command.title.strip()
        )
        or command.action == "set_status"
        and (item["id"] != command.id or item["status"] != command.status)
        or command.action == "delete"
        and item["id"] != command.id
    ):
        raise ValueError("待办记录无效")


def validate_delete_confirmation(
    command: TodoCommand, items: list[dict], requests: list[dict], choices: list[dict]
) -> None:
    if command.action != "delete":
        return
    item = next((item for item in items if item["id"] == command.id), None)
    if (
        len(requests) != 1
        or len(choices) != 1
        or choices[0]["decision"] != "approved"
        or item is None
        or requests[0]["task"] != {"id": item["id"], "title": item["title"]}
    ):
        raise ValueError("待办删除尚未确认")


def confirmation_request(command: TodoCommand, items: list[dict]) -> dict | None:
    if command.action != "delete":
        return None
    item = next((item for item in items if item["id"] == command.id), None)
    if item is None:
        raise ValueError("待办任务不存在")
    return {
        "name": "todo",
        "action": "delete",
        "task": {"id": item["id"], "title": item["title"]},
    }


def shared_snapshot(path: Path, lock: FileLock) -> list[dict]:
    with lock:
        return load_shared(path)[0]


def stage_change(
    command: TodoCommand,
    items: list[dict],
    next_id: int,
    *,
    start: Callable[[], bool] | None = None,
) -> tuple[str | None, TodoList]:
    store = restore(items, next_id)
    return apply_command(store, command, start=start), store


def commit_shared(path: Path, command: TodoCommand, store: TodoList, next_id: int) -> None:
    if command.action != "list" or not path.exists():
        save_shared(path, snapshot(store), next_id + (command.action == "add"))


def load_shared(path: Path) -> tuple[list[dict], int]:
    if not path.exists():
        return [], 1
    data = read_bytes(path)
    if not data or not data.endswith(b"\n"):
        raise ValueError("待办文件残缺")
    lines = data.splitlines()
    header = json.loads(lines[0])
    if not isinstance(header, dict) or set(header) != {"next_id"}:
        raise ValueError("待办文件无效")
    items = [TodoItem.model_validate_json(line).model_dump() for line in lines[1:]]
    store = restore(items, header["next_id"])
    return snapshot(store), header["next_id"]


def save_shared(path: Path, items: list[dict], next_id: int) -> None:
    path.parent.mkdir(exist_ok=True)
    temporary = path.parent / f"{uuid4().hex}.tmp"
    data = "".join(
        json.dumps(value, ensure_ascii=False) + "\n"
        for value in ({"next_id": next_id}, *items)
    ).encode("utf-8")
    try:
        write_bytes(temporary, data, "create")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
