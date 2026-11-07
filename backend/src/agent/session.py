import asyncio
import json
import os
from collections.abc import Callable
from pathlib import Path
from threading import RLock
from uuid import uuid4

from filelock import FileLock
from pydantic import JsonValue, TypeAdapter

from .security import check, create_guard
from .tools.registry import ToolRegistry, execute as execute_registered_tool
from .tools.todo import TodoCommand, validate_delete_confirmation
from .tools.todo_store import SharedTodo
from .types import (
    AgentMessage,
    AssistantMessage,
    SystemMessage,
    ToolCallContent,
)
from .session_records import (
    EventRecord,
    ExecutionEvent,
    MessageRecord,
    Record,
    SessionHeader,
    SessionInfoRecord,
    TERMINAL as _TERMINAL,
    parse_records,
    timestamp as _now,
    validate_event as _validate_event,
    validate_id as _id,
    validate_steering as _validate_steering,
    validate_todo_results as _validate_todo_results,
    validate_tool_results as _validate_tool_results,
)
from ..ai.models.openai import to_openai_messages


_LIVE_RUNS_LOCK = RLock()
_LIVE_RUNS: set[tuple[str, str]] = set()
_LIVE_LOCKS: dict[str, RLock] = {}
_WRITE_FAILURES: set[str] = set()


def release_run(session: "Session", run_id: str) -> None:
    try:
        with session.live_lock, _LIVE_RUNS_LOCK:
            _LIVE_RUNS.discard((str(session.path), run_id))
    finally:
        session.run_lock.release()


class Session:
    def __init__(self, root: Path, session_id: str, secrets: tuple[str, ...]) -> None:
        self.root = root.resolve()
        self.id = _id(session_id)
        self.guard = create_guard(secrets)
        self.path = self.root / f"{self.id}.jsonl"
        if self.path.resolve().parent != self.root or self.path.is_symlink():
            raise ValueError("无效会话路径")
        self.lock = FileLock(str(self.root / ".locks" / f"{self.id}.lock"))
        self.stream_lock = FileLock(
            str(self.root / ".locks" / f"{self.id}.stream.lock")
        )
        self.tool_lock = FileLock(str(self.root / ".locks" / f"{self.id}.tool.lock"))
        self.todo = SharedTodo(self.root)
        self.todo_lock = self.todo.lock
        self.todo_path = self.todo.path
        self.run_lock = FileLock(
            str(self.root / ".locks" / f"{self.id}.run.lock"), thread_local=False
        )
        with _LIVE_RUNS_LOCK:
            if str(self.path) not in _LIVE_LOCKS:
                _LIVE_LOCKS[str(self.path)] = RLock()
            self.live_lock = _LIVE_LOCKS[str(self.path)]

    def _read(self) -> list[Record]:
        if str(self.path) in _WRITE_FAILURES:
            raise OSError("会话写入状态不确定")
        return parse_records(self.path.read_bytes(), self.id)

    def read(self) -> list[Record]:
        with self.lock:
            return self._read()

    def _append(self, record: Record, *, ids: set[str] | None = None) -> None:
        if str(self.path) in _WRITE_FAILURES:
            raise OSError("会话写入状态不确定")
        if ids is None:
            ids = {existing.id for existing in self._read()}
        if record.id in ids:
            raise ValueError("记录 ID 重复")
        value = record.model_dump(mode="json")
        if isinstance(record, MessageRecord) and record.queue_id is None:
            del value["queue_id"]
        check(self.guard, value)
        file = self.path.open("ab")
        committed = False
        try:
            with file:
                file.write(
                    (
                        json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n"
                    ).encode("utf-8")
                )
                file.flush()
                os.fsync(file.fileno())
            committed = True
        finally:
            if not committed:
                _WRITE_FAILURES.add(str(self.path))

    def append_model_record(self, record: EventRecord, *, ids: set[str]) -> None:
        with self.lock:
            if record.event.type not in {"model.delta", "model.response"}:
                raise ValueError("模型流记录类型无效")
            _validate_event(record.event, {})
            self._append(record, ids=ids)

    def set_title(self, title: str) -> SessionInfoRecord:
        check(self.guard, title)
        with self.lock:
            self._read()
            record = SessionInfoRecord(id=uuid4().hex, timestamp=_now(), title=title)
            self._append(record)
            return record

    def append_message(
        self, message: AgentMessage, run_id: str | None, *, queue_id: str | None = None
    ) -> MessageRecord:
        check(self.guard, message)
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, ToolCallContent):
                    check(
                        self.guard,
                        TypeAdapter(JsonValue).validate_json(block.arguments),
                    )
        with self.lock:
            records = self._read()
            if run_id is None:
                if not isinstance(message, SystemMessage):
                    raise ValueError("运行外消息必须是系统消息")
            else:
                _id(run_id)
                events = [
                    r.event
                    for r in records
                    if isinstance(r, EventRecord) and r.event.run_id == run_id
                ]
                if not events or events[-1].type in _TERMINAL:
                    raise ValueError("消息所属运行无效")
            record = MessageRecord(
                id=uuid4().hex,
                timestamp=_now(),
                run_id=run_id,
                message=message,
                queue_id=queue_id,
            )
            _validate_steering([*records, record])
            _validate_tool_results([*records, record])
            self._append(record)
            return record

    def append_event(
        self, run_id: str, event_type: str, data: dict[str, JsonValue]
    ) -> ExecutionEvent:
        check(self.guard, data)
        if event_type in {"model.delta", "model.response"}:
            raise ValueError("模型流事件须经过跨片段密钥检查")
        if event_type in {"tool.started", "tool.completed"} and isinstance(
            data.get("arguments"), str
        ):
            check(self.guard, TypeAdapter(JsonValue).validate_json(data["arguments"]))
        if event_type == "tool.completed" and data.get("name") == "todo":
            raise ValueError("待办事件须通过待办接口提交")
        with self.lock:
            records = self._read()
            event = self._append_event(records, run_id, event_type, data)
            if (
                event_type == "tool.confirmation_resolved"
                and data.get("decision") == "rejected"
            ):
                self._append_event(
                    self._read(), run_id, "run.finished", {"status": "stopped"}
                )
            return event

    def _append_event(
        self,
        records: list[Record],
        run_id: str,
        event_type: str,
        data: dict[str, JsonValue],
    ) -> ExecutionEvent:
        _id(run_id)
        events = [
            r.event
            for r in records
            if isinstance(r, EventRecord) and r.event.run_id == run_id
        ]
        if events and events[-1].type in _TERMINAL:
            raise ValueError("运行已经结束")
        if (not events) != (event_type == "run.started"):
            raise ValueError("运行开始事件无效")
        if not events:
            last = {
                r.event.run_id: r.event for r in records if isinstance(r, EventRecord)
            }
            if any(event.type not in _TERMINAL for event in last.values()):
                raise ValueError("会话已有运行")
        if (
            event_type == "model.request"
            and sum(event.type == "model.request" for event in events) >= 64
        ):
            raise ValueError("模型请求次数达到上限")
        now = _now()
        event = ExecutionEvent(
            run_id=run_id, seq=len(events) + 1, time=now, type=event_type, data=data
        )
        confirmations: dict[tuple[str, int, str], bool] = {}
        for record in records:
            if isinstance(record, EventRecord):
                _validate_event(record.event, confirmations)
        _validate_event(event, confirmations)
        record = EventRecord(id=uuid4().hex, timestamp=now, event=event)
        _validate_steering([*records, record])
        _validate_tool_results([*records, record])
        _validate_todo_results([*records, record])
        self._append(record, ids={existing.id for existing in records})
        return event

    def execute_tool(
        self,
        registry: ToolRegistry,
        run_id: str,
        turn_index: int,
        tool_call_id: str,
        name: str,
        arguments: str,
        *,
        start: Callable[[], bool] | None = None,
    ) -> str | None:
        _id(run_id)
        if (
            type(turn_index) is not int
            or turn_index < 1
            or not isinstance(tool_call_id, str)
            or not tool_call_id
        ):
            raise ValueError("无效工具调用")
        check(self.guard, {"name": name, "arguments": arguments})
        tool = registry.get(name)
        if name == "todo" and tool is not None and not tool.contextual:
            raise ValueError("待办工具需要持久化执行器")
        if tool is not None and tool.contextual:
            if getattr(tool.execute, "__self__", None) is not self:
                raise ValueError("工具执行器与会话不匹配")
            return execute_registered_tool(
                registry,
                name,
                arguments,
                secrets=self.guard.secrets,
                start=start,
                context=(run_id, turn_index, tool_call_id),
            )
        with self.tool_lock:
            with self.lock:
                records = self._read()
                events = [
                    r.event
                    for r in records
                    if isinstance(r, EventRecord) and r.event.run_id == run_id
                ]
                if not events or events[-1].type in _TERMINAL:
                    raise ValueError("运行已经结束")
                matches = lambda event: (
                    event.data.get("turn_index") == turn_index
                    and event.data.get("tool_call_id") == tool_call_id
                )
                if not any(
                    event.type == "tool.started"
                    and matches(event)
                    and event.data.get("name") == name
                    and event.data.get("arguments") == arguments
                    for event in events
                ):
                    raise ValueError("工具尚未启动")
                if any(
                    event.type == "tool.completed" and matches(event)
                    for event in events
                ):
                    raise ValueError("工具调用已经完成")
            result = execute_registered_tool(
                registry, name, arguments, secrets=self.guard.secrets, start=start
            )
            if result is None:
                return None
            check(self.guard, result)
            with self.lock:
                self._append_event(
                    self._read(),
                    run_id,
                    "tool.completed",
                    {
                        "turn_index": turn_index,
                        "tool_call_id": tool_call_id,
                        "name": name,
                        "arguments": arguments,
                        "result": result,
                    },
                )
            return result

    def todo_snapshot(self) -> list[dict]:
        return self.todo.snapshot()

    def execute_todo(
        self,
        run_id: str,
        turn_index: int,
        tool_call_id: str,
        arguments: str,
        *,
        start: Callable[[], bool] | None = None,
    ) -> str | None:
        _id(run_id)
        if (
            type(turn_index) is not int
            or turn_index < 1
            or not isinstance(tool_call_id, str)
            or not tool_call_id
        ):
            raise ValueError("无效工具调用")
        check(self.guard, arguments)
        check(self.guard, TypeAdapter(JsonValue).validate_json(arguments))
        command = TodoCommand.model_validate_json(arguments)
        check(self.guard, command)
        with self.todo_lock, self.tool_lock:
            with self.lock:
                records = self._read()
                events = [
                    r.event
                    for r in records
                    if isinstance(r, EventRecord) and r.event.run_id == run_id
                ]
                if not events or events[-1].type in _TERMINAL:
                    raise ValueError("运行已经结束")
                matches = lambda event: (
                    event.data.get("turn_index") == turn_index
                    and event.data.get("tool_call_id") == tool_call_id
                )
                if not any(
                    event.type == "tool.started"
                    and matches(event)
                    and event.data.get("name") == "todo"
                    and event.data.get("arguments") == arguments
                    for event in events
                ):
                    raise ValueError("待办工具尚未启动")
                if any(
                    event.type == "tool.completed" and matches(event)
                    for event in events
                ):
                    raise ValueError("工具调用已经完成")
                items, next_id = self.todo.read()
                if command.action == "delete":
                    requests = [
                        event.data
                        for event in events
                        if event.type == "tool.confirmation_requested"
                        and matches(event)
                    ]
                    choices = [
                        event.data
                        for event in events
                        if event.type == "tool.confirmation_resolved" and matches(event)
                    ]
                    validate_delete_confirmation(command, items, requests, choices)
            result, store = self.todo.execute(command, items, next_id, start=start)
            if result is None:
                return None
            check(self.guard, result)
            with self.lock:
                records = self._read()
                events = [
                    r.event
                    for r in records
                    if isinstance(r, EventRecord) and r.event.run_id == run_id
                ]
                if not events or events[-1].type in _TERMINAL:
                    raise ValueError("运行已经结束")
                self.todo.commit(command, store, next_id)
                self._append_event(
                    records,
                    run_id,
                    "tool.completed",
                    {
                        "turn_index": turn_index,
                        "tool_call_id": tool_call_id,
                        "name": "todo",
                        "arguments": arguments,
                        "result": result,
                    },
                )
            return result

    def trace(self, run_id: str) -> list[ExecutionEvent]:
        _id(run_id)
        return [
            r.event
            for r in self.read()
            if isinstance(r, EventRecord) and r.event.run_id == run_id
        ]

    def recover(self) -> list[ExecutionEvent]:
        with _LIVE_RUNS_LOCK:
            if any(path == str(self.path) for path, _ in _LIVE_RUNS):
                return []
        with self.run_lock.acquire(blocking=False), self.lock, self.live_lock:
            with _LIVE_RUNS_LOCK:
                if any(path == str(self.path) for path, _ in _LIVE_RUNS):
                    return []
            records = self._read()
            last = {
                r.event.run_id: r.event for r in records if isinstance(r, EventRecord)
            }
            return [
                self._append_event(
                    records, run_id, "run.finished", {"status": "interrupted"}
                )
                for run_id, event in last.items()
                if event.type not in _TERMINAL
            ]

    def context(self, active_run_id: str) -> list[AgentMessage]:
        _id(active_run_id)
        records = self.read()
        completed = {
            r.event.run_id
            for r in records
            if isinstance(r, EventRecord) and r.event.type == "run.completed"
        }
        applied = {
            r.event.data["message_id"]
            for r in records
            if isinstance(r, EventRecord) and r.event.type == "steer.applied"
        }
        messages = [
            r.message
            for r in records
            if isinstance(r, MessageRecord)
            and (r.run_id is None or r.run_id == active_run_id or r.run_id in completed)
            and (r.queue_id is None or r.id in applied)
        ]
        check(self.guard, messages)
        to_openai_messages(messages)
        return messages


async def await_task(task: asyncio.Task):
    cancellation = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as error:
            if cancellation is None:
                cancellation = error
    result = task.result()
    if cancellation is not None:
        raise cancellation
    return result


async def run_sync(function: Callable, *args, **kwargs):
    return await await_task(
        asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    )


class SessionManager:
    def __init__(
        self,
        root: Path = Path(__file__).resolve().parents[2] / ".data" / "sessions",
        *,
        secrets: tuple[str, ...],
    ) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / ".locks").mkdir(exist_ok=True)
        self.secrets = secrets

    def create(self) -> Session:
        session = Session(self.root, uuid4().hex, self.secrets)
        with session.lock:
            header = SessionHeader(version=1, id=session.id, created_at=_now())
            check(session.guard, header.model_dump(mode="json"))
            with session.path.open("xb") as file:
                file.write((header.model_dump_json() + "\n").encode("utf-8"))
                file.flush()
                os.fsync(file.fileno())
        return session

    def open(self, session_id: str) -> Session:
        session = Session(self.root, session_id, self.secrets)
        session.read()
        return session

    def recover(self) -> dict[str, list[ExecutionEvent]]:
        return {
            session_id: self.open(session_id).recover() for session_id, _ in self.list()
        }

    def list(self) -> list[tuple[str, str]]:
        result = []
        for path in sorted(self.root.glob("*.jsonl")):
            session = self.open(path.stem)
            titles = [
                r.title for r in session.read() if isinstance(r, SessionInfoRecord)
            ]
            result.append((session.id, titles[-1] if titles else ""))
        return result
