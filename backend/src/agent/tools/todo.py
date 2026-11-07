"""内存任务清单：单实例内 ID 递增，读写加锁并返回快照，进程退出即丢失。"""

import json
from collections.abc import Callable
from threading import Lock
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

TodoStatus = Literal["pending", "in_progress", "completed"]
TITLE_MAX_LENGTH = 200


class TodoItem(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=TITLE_MAX_LENGTH)
    status: TodoStatus


class TodoCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    action: Literal["add", "list", "set_status", "delete"]
    title: str | None = Field(default=None, min_length=1, max_length=TITLE_MAX_LENGTH, description="任务标题，add 时必填")
    id: int | None = Field(default=None, ge=1, description="任务 ID，set_status 与 delete 时必填")
    status: TodoStatus | None = Field(default=None, description="set_status 时必填")


class TodoList:
    def __init__(self) -> None:
        self._lock = Lock()
        self._items: dict[int, dict] = {}
        self._next_id = 1

    @classmethod
    def restore(cls, items: list[dict], next_id: int) -> "TodoList":
        validated = [TodoItem.model_validate(item).model_dump() for item in items]
        ids = {item["id"] for item in validated}
        if len(ids) != len(validated) or type(next_id) is not int or next_id < 1 or (ids and next_id <= max(ids)):
            raise ValueError("无效待办快照")
        store = cls()
        store._items = {item["id"]: item for item in validated}
        store._next_id = next_id
        return store

    def add(self, title: str) -> dict:
        with self._lock:
            item = {"id": self._next_id, "title": title, "status": "pending"}
            self._items[self._next_id] = item
            self._next_id += 1
            return dict(item)

    def snapshot(self) -> list[dict]:
        with self._lock:
            return [dict(item) for item in self._items.values()]

    def set_status(self, item_id: int, status: TodoStatus) -> dict:
        with self._lock:
            item = self._items.get(item_id)
            if item is None:
                raise ValueError(f"任务不存在: {item_id}")
            item["status"] = status
            return dict(item)

    def delete(self, item_id: int) -> dict:
        with self._lock:
            item = self._items.pop(item_id, None)
            if item is None:
                raise ValueError(f"任务不存在: {item_id}")
            return dict(item)

    def execute(self, command: TodoCommand, *, start: Callable[[], bool] | None = None) -> str | None:
        if start is not None and not start():
            return None
        match command.action:
            case "add":
                if command.title is None:
                    raise ValueError("add 需要 title")
                title = command.title.strip()
                if not title:
                    raise ValueError("标题不能为空")
                return json.dumps({"item": self.add(title)}, ensure_ascii=False)
            case "list":
                return json.dumps({"items": self.snapshot()}, ensure_ascii=False)
            case "set_status":
                if command.id is None or command.status is None:
                    raise ValueError("set_status 需要 id 与 status")
                return json.dumps({"item": self.set_status(command.id, command.status)}, ensure_ascii=False)
            case "delete":
                if command.id is None:
                    raise ValueError("delete 需要 id")
                return json.dumps({"deleted": self.delete(command.id)}, ensure_ascii=False)
