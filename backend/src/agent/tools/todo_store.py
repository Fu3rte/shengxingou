import json
import os
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

from filelock import FileLock

from .todo import TodoCommand, TodoItem, TodoList, execute, restore, snapshot


class SharedTodo:
    def __init__(self, root: Path) -> None:
        self.path = root / ".todo" / "todo.jsonl"
        self.lock = FileLock(str(root / ".locks" / "todo.lock"))

    def read(self) -> tuple[list[dict], int]:
        return read(self.path)

    def snapshot(self) -> list[dict]:
        with self.lock:
            return self.read()[0]

    def execute(
        self,
        command: TodoCommand,
        items: list[dict],
        next_id: int,
        *,
        start: Callable[[], bool] | None = None,
    ) -> tuple[str | None, TodoList]:
        store = restore(items, next_id)
        return execute(store, command, start=start), store

    def commit(self, command: TodoCommand, store: TodoList, next_id: int) -> None:
        if command.action != "list" or not self.path.exists():
            write(self.path, snapshot(store), next_id + (command.action == "add"))


def read(path: Path) -> tuple[list[dict], int]:
    if not path.exists():
        return [], 1
    data = path.read_bytes()
    if not data or not data.endswith(b"\n"):
        raise ValueError("待办文件残缺")
    lines = data.splitlines()
    header = json.loads(lines[0])
    if not isinstance(header, dict) or set(header) != {"next_id"}:
        raise ValueError("待办文件无效")
    items = [TodoItem.model_validate_json(line).model_dump() for line in lines[1:]]
    store = restore(items, header["next_id"])
    return snapshot(store), header["next_id"]


def write(path: Path, items: list[dict], next_id: int) -> None:
    path.parent.mkdir(exist_ok=True)
    temporary = path.parent / f"{uuid4().hex}.tmp"
    try:
        with temporary.open("xb") as file:
            for value in ({"next_id": next_id}, *items):
                file.write(
                    (json.dumps(value, ensure_ascii=False) + "\n").encode("utf-8")
                )
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
