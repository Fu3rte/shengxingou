import json
import os
import re
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal
from uuid import uuid4

from filelock import FileLock
from openai.types.chat import ChatCompletionChunk
from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter

from .security import SecretGuard
from .tools.registry import ToolRegistry
from .tools.todo import TodoCommand, TodoItem, TodoList
from .types import AgentMessage, AssistantMessage, SystemMessage, ToolResultMessage
from ..ai.models.normalized import normalized_stream
from ..ai.models.openai import to_openai_messages


class RecordModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class SessionHeader(RecordModel):
    type: Literal["session"] = "session"
    version: int = Field(ge=1, le=1)
    id: str
    created_at: str


class MessageRecord(RecordModel):
    type: Literal["message"] = "message"
    id: str
    timestamp: str
    run_id: str | None
    message: AgentMessage


class SessionInfoRecord(RecordModel):
    type: Literal["session_info"] = "session_info"
    id: str
    timestamp: str
    title: str


class ExecutionEvent(RecordModel):
    run_id: str
    seq: int = Field(ge=1)
    time: str
    type: Literal[
        "run.started", "model.request", "model.delta", "model.response",
        "tool.started", "tool.completed", "tool.confirmation_requested",
        "tool.confirmation_resolved", "steer.queued", "steer.applied",
        "run.completed", "run.finished",
    ]
    data: dict[str, JsonValue]


class EventRecord(RecordModel):
    type: Literal["event"] = "event"
    id: str
    timestamp: str
    event: ExecutionEvent


class ConfirmationTask(RecordModel):
    id: int = Field(ge=1)
    title: str


class ConfirmationRequested(RecordModel):
    turn_index: int = Field(ge=1)
    tool_call_id: str
    name: Literal["todo"]
    action: Literal["delete"]
    task: ConfirmationTask


class ConfirmationResolved(RecordModel):
    turn_index: int = Field(ge=1)
    tool_call_id: str
    decision: Literal["approved", "rejected"]


Record = Annotated[SessionHeader | MessageRecord | SessionInfoRecord | EventRecord, Field(discriminator="type")]
_RECORD = TypeAdapter(Record)
_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_TERMINAL = {"run.completed", "run.finished"}


def _id(value: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError("无效 ID")
    return value


def _utc(value: str) -> None:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise ValueError("时间必须为 UTC")


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _validate_event(event: ExecutionEvent, confirmations: dict[tuple[str, int, str], bool]) -> None:
    data = event.data
    if event.type.startswith(("model.", "tool.")):
        if type(data.get("turn_index")) is not int or data["turn_index"] < 1:
            raise ValueError("无效 Turn 序号")
    if event.type == "run.started" and not isinstance(data.get("input"), str):
        raise ValueError("无效运行输入")
    if event.type == "run.completed" and not isinstance(data.get("answer"), str):
        raise ValueError("无效运行结果")
    if event.type == "run.finished" and data.get("status") not in {"stopped", "limited", "failed", "interrupted"}:
        raise ValueError("无效运行状态")
    if event.type == "model.request" and (set(data) != {"turn_index", "model", "messages", "tools"}
                                          or not isinstance(data.get("model"), str)
                                          or not isinstance(data.get("messages"), list)
                                          or not isinstance(data.get("tools"), list)):
        raise ValueError("无效模型请求")
    if event.type == "model.delta":
        fragments = data.get("fragments")
        if set(data) != {"turn_index", "fragments"} or not isinstance(fragments, dict) or not fragments or any(
            not isinstance(key, str) or not isinstance(value, str)
            or (key != "text" and not re.fullmatch(r"tool:\d+:(id|name|arguments)", key))
            for key, value in fragments.items()
        ):
            raise ValueError("无效模型流事件")
    if event.type == "model.response":
        AssistantMessage.model_validate(data.get("message"))
        if set(data) != {"turn_index", "message", "finish_reason", "usage"} or data.get("finish_reason") not in {"stop", "tool_calls"} or (
            data.get("usage") is not None and not isinstance(data.get("usage"), dict)
        ):
            raise ValueError("无效模型响应")
    if event.type in {"tool.started", "tool.completed"}:
        keys = ("tool_call_id", "name", "arguments")
        if event.type == "tool.completed":
            keys += ("result",)
        if (set(data) != {"turn_index", *keys} or any(not isinstance(data.get(key), str) for key in keys)
                or not data["tool_call_id"] or not data["name"]):
            raise ValueError("无效工具事件")
    if event.type == "tool.confirmation_requested":
        request = ConfirmationRequested.model_validate(data)
        key = (event.run_id, request.turn_index, request.tool_call_id)
        if key in confirmations:
            raise ValueError("确认请求重复")
        confirmations[key] = False
    if event.type == "tool.confirmation_resolved":
        resolution = ConfirmationResolved.model_validate(data)
        key = (event.run_id, resolution.turn_index, resolution.tool_call_id)
        if key not in confirmations or confirmations[key]:
            raise ValueError("确认请求无效")
        confirmations[key] = True


def _validate_tool_results(records: list[Record]) -> None:
    pending: dict[tuple[str, str], str] = {}
    for record in records:
        if isinstance(record, EventRecord) and record.event.type == "tool.completed":
            event = record.event
            key = (event.run_id, event.data["tool_call_id"])
            if key in pending:
                raise ValueError("工具结果重复")
            pending[key] = event.data["result"]
        if isinstance(record, MessageRecord) and isinstance(record.message, ToolResultMessage):
            key = (record.run_id, record.message.tool_call_id)
            if key not in pending or pending[key] != record.message.content:
                raise ValueError("工具结果未持久化")
            del pending[key]
        if isinstance(record, EventRecord) and record.event.type == "run.completed":
            if any(run_id == record.event.run_id for run_id, _ in pending):
                raise ValueError("运行缺少工具结果消息")


def _todo_state(records: list[Record]) -> tuple[list[dict], int]:
    items: dict[int, dict] = {}
    next_id = 1
    for record in records:
        if not isinstance(record, EventRecord) or record.event.type != "tool.completed":
            continue
        data = record.event.data
        if data.get("name") != "todo":
            continue
        command = TodoCommand.model_validate_json(data["arguments"])
        result = json.loads(data["result"])
        if command.action == "list":
            if not isinstance(result, dict) or set(result) != {"items"} or not isinstance(result["items"], list):
                raise ValueError("待办记录无效")
            listed = [TodoItem.model_validate(item).model_dump() for item in result["items"]]
            if listed != list(items.values()):
                raise ValueError("待办记录无效")
            continue
        key = "deleted" if command.action == "delete" else "item"
        if not isinstance(result, dict) or set(result) != {key}:
            raise ValueError("待办记录无效")
        item = TodoItem.model_validate(result[key]).model_dump()
        if command.action == "add":
            if command.title is None or item != {"id": next_id, "title": command.title.strip(), "status": "pending"}:
                raise ValueError("待办记录无效")
            items[next_id] = item
            next_id += 1
        elif command.action == "set_status":
            if command.id not in items or command.status is None or item != {**items[command.id], "status": command.status}:
                raise ValueError("待办记录无效")
            items[command.id] = item
        elif command.action == "delete":
            if command.id not in items or item != items[command.id]:
                raise ValueError("待办记录无效")
            del items[command.id]
    return list(items.values()), next_id


class Session:
    def __init__(self, root: Path, session_id: str, secrets: tuple[str, ...]) -> None:
        self.root = root.resolve()
        self.id = _id(session_id)
        self.guard = SecretGuard(secrets)
        self.path = self.root / f"{self.id}.jsonl"
        if self.path.resolve().parent != self.root or self.path.is_symlink():
            raise ValueError("无效会话路径")
        self.lock = FileLock(str(self.root / ".locks" / f"{self.id}.lock"))
        self.stream_lock = FileLock(str(self.root / ".locks" / f"{self.id}.stream.lock"))

    def _read(self) -> list[Record]:
        data = self.path.read_bytes()
        if not data or not data.endswith(b"\n"):
            raise ValueError("会话记录残缺")
        records = [_RECORD.validate_json(line) for line in data.splitlines()]
        if not isinstance(records[0], SessionHeader) or records[0].id != self.id:
            raise ValueError("无效会话头")
        seen: set[str] = set()
        runs: dict[str, tuple[int, bool]] = {}
        confirmations: dict[tuple[str, int, str], bool] = {}
        for index, record in enumerate(records):
            _id(record.id)
            if record.id in seen:
                raise ValueError("记录 ID 重复")
            seen.add(record.id)
            _utc(record.created_at if isinstance(record, SessionHeader) else record.timestamp)
            if index and isinstance(record, SessionHeader):
                raise ValueError("会话头重复")
            if isinstance(record, MessageRecord):
                if record.run_id is None:
                    if not isinstance(record.message, SystemMessage):
                        raise ValueError("运行外消息必须是系统消息")
                else:
                    _id(record.run_id)
                    if record.run_id not in runs or runs[record.run_id][1]:
                        raise ValueError("消息所属运行无效")
            if isinstance(record, EventRecord):
                event = record.event
                _id(event.run_id)
                _utc(event.time)
                previous, terminal = runs.get(event.run_id, (0, False))
                if terminal or event.seq != previous + 1 or ((previous == 0) != (event.type == "run.started")):
                    raise ValueError("运行事件顺序无效")
                if previous == 0 and any(not ended for _, ended in runs.values()):
                    raise ValueError("会话已有运行")
                _validate_event(event, confirmations)
                runs[event.run_id] = (event.seq, event.type in _TERMINAL)
        _validate_tool_results(records)
        _todo_state(records)
        return records

    def read(self) -> list[Record]:
        with self.lock:
            return self._read()

    def _append(self, record: Record) -> None:
        if any(existing.id == record.id for existing in self._read()):
            raise ValueError("记录 ID 重复")
        value = record.model_dump(mode="json")
        self.guard.check(value)
        with self.path.open("ab") as file:
            file.write((json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8"))
            file.flush()
            os.fsync(file.fileno())

    def set_title(self, title: str) -> SessionInfoRecord:
        self.guard.check(title)
        with self.lock:
            self._read()
            record = SessionInfoRecord(id=uuid4().hex, timestamp=_now(), title=title)
            self._append(record)
            return record

    def append_message(self, message: AgentMessage, run_id: str | None) -> MessageRecord:
        self.guard.check(message)
        with self.lock:
            records = self._read()
            if run_id is None:
                if not isinstance(message, SystemMessage):
                    raise ValueError("运行外消息必须是系统消息")
            else:
                _id(run_id)
                events = [r.event for r in records if isinstance(r, EventRecord) and r.event.run_id == run_id]
                if not events or events[-1].type in _TERMINAL:
                    raise ValueError("消息所属运行无效")
            record = MessageRecord(id=uuid4().hex, timestamp=_now(), run_id=run_id, message=message)
            _validate_tool_results([*records, record])
            self._append(record)
            return record

    def append_event(self, run_id: str, event_type: str, data: dict[str, JsonValue]) -> ExecutionEvent:
        self.guard.check(data)
        if event_type in {"model.delta", "model.response"}:
            raise ValueError("模型流事件须经过跨片段密钥检查")
        if event_type == "tool.completed" and data.get("name") == "todo":
            raise ValueError("待办事件须通过待办接口提交")
        with self.lock:
            records = self._read()
            event = self._append_event(records, run_id, event_type, data)
            if event_type == "tool.confirmation_resolved" and data.get("decision") == "rejected":
                self._append_event(self._read(), run_id, "run.finished", {"status": "stopped"})
            return event

    def _append_event(self, records: list[Record], run_id: str, event_type: str, data: dict[str, JsonValue]) -> ExecutionEvent:
        _id(run_id)
        events = [r.event for r in records if isinstance(r, EventRecord) and r.event.run_id == run_id]
        if events and events[-1].type in _TERMINAL:
            raise ValueError("运行已经结束")
        if (not events) != (event_type == "run.started"):
            raise ValueError("运行开始事件无效")
        if not events:
            last = {r.event.run_id: r.event for r in records if isinstance(r, EventRecord)}
            if any(event.type not in _TERMINAL for event in last.values()):
                raise ValueError("会话已有运行")
        if event_type == "model.request" and sum(event.type == "model.request" for event in events) >= 64:
            raise ValueError("模型请求次数达到上限")
        now = _now()
        event = ExecutionEvent(run_id=run_id, seq=len(events) + 1, time=now, type=event_type, data=data)
        confirmations: dict[tuple[str, int, str], bool] = {}
        for record in records:
            if isinstance(record, EventRecord):
                _validate_event(record.event, confirmations)
        _validate_event(event, confirmations)
        record = EventRecord(id=uuid4().hex, timestamp=now, event=event)
        _validate_tool_results([*records, record])
        _todo_state([*records, record])
        self._append(record)
        return event

    def record_model_stream(self, run_id: str, turn_index: int, chunks: Iterable[ChatCompletionChunk]) -> Iterator[ExecutionEvent]:
        if type(turn_index) is not int or turn_index < 1:
            raise ValueError("无效 Turn 序号")
        with self.stream_lock:
            with self.lock:
                records = self._read()
                events = [r.event for r in records if isinstance(r, EventRecord) and r.event.run_id == run_id]
                if not events or events[-1].type in _TERMINAL or not any(
                    event.type == "model.request" and event.data.get("turn_index") == turn_index for event in events
                ) or any(event.type in {"model.delta", "model.response"} and event.data.get("turn_index") == turn_index
                         for event in events):
                    raise ValueError("模型请求状态无效")
            for normalized in normalized_stream(chunks, self.guard.secrets):
                with self.lock:
                    records = self._read()
                    data = {"turn_index": turn_index, **normalized["data"]}
                    event = self._append_event(records, run_id, normalized["type"], data)
                yield event

    def execute_tool(self, registry: ToolRegistry, run_id: str, turn_index: int, tool_call_id: str,
                     name: str, arguments: str) -> str:
        _id(run_id)
        if type(turn_index) is not int or turn_index < 1 or not isinstance(tool_call_id, str) or not tool_call_id:
            raise ValueError("无效工具调用")
        if name == "todo":
            return self.execute_todo(run_id, turn_index, tool_call_id, arguments)
        self.guard.check({"name": name, "arguments": arguments})
        with self.lock:
            records = self._read()
            events = [r.event for r in records if isinstance(r, EventRecord) and r.event.run_id == run_id]
            if not events or events[-1].type in _TERMINAL:
                raise ValueError("运行已经结束")
            matches = lambda event: (event.data.get("turn_index") == turn_index
                                     and event.data.get("tool_call_id") == tool_call_id)
            if not any(event.type == "tool.started" and matches(event) and event.data.get("name") == name
                       and event.data.get("arguments") == arguments for event in events):
                raise ValueError("工具尚未启动")
            if any(event.type == "tool.completed" and matches(event) for event in events):
                raise ValueError("工具调用已经完成")
            result = registry.execute(name, arguments)
            self.guard.check(result)
            self._append_event(records, run_id, "tool.completed", {
                "turn_index": turn_index, "tool_call_id": tool_call_id, "name": name,
                "arguments": arguments, "result": result,
            })
            return result

    def todo_snapshot(self) -> list[dict]:
        return _todo_state(self.read())[0]

    def execute_todo(self, run_id: str, turn_index: int, tool_call_id: str, arguments: str) -> str:
        _id(run_id)
        if type(turn_index) is not int or turn_index < 1 or not isinstance(tool_call_id, str) or not tool_call_id:
            raise ValueError("无效工具调用")
        self.guard.check(arguments)
        command = TodoCommand.model_validate_json(arguments)
        with self.lock:
            records = self._read()
            events = [r.event for r in records if isinstance(r, EventRecord) and r.event.run_id == run_id]
            if not events or events[-1].type in _TERMINAL:
                raise ValueError("运行已经结束")
            matches = lambda event: (event.data.get("turn_index") == turn_index
                                     and event.data.get("tool_call_id") == tool_call_id)
            if not any(event.type == "tool.started" and matches(event) and event.data.get("name") == "todo"
                       and event.data.get("arguments") == arguments for event in events):
                raise ValueError("待办工具尚未启动")
            if any(event.type == "tool.completed" and matches(event) for event in events):
                raise ValueError("工具调用已经完成")
            items, next_id = _todo_state(records)
            if command.action == "delete":
                requests = [event for event in events if event.type == "tool.confirmation_requested" and matches(event)]
                choices = [event for event in events if event.type == "tool.confirmation_resolved" and matches(event)]
                item = next((item for item in items if item["id"] == command.id), None)
                if (len(requests) != 1 or len(choices) != 1 or choices[0].data["decision"] != "approved"
                        or item is None or requests[0].data["task"] != {"id": item["id"], "title": item["title"]}):
                    raise ValueError("待办删除尚未确认")
            result = TodoList.restore(items, next_id).execute(command)
            self.guard.check(result)
            self._append_event(records, run_id, "tool.completed", {
                "turn_index": turn_index, "tool_call_id": tool_call_id, "name": "todo",
                "arguments": arguments, "result": result,
            })
            return result

    def trace(self, run_id: str) -> list[ExecutionEvent]:
        _id(run_id)
        return [r.event for r in self.read() if isinstance(r, EventRecord) and r.event.run_id == run_id]

    def recover(self) -> list[ExecutionEvent]:
        with self.lock:
            records = self._read()
            last = {r.event.run_id: r.event for r in records if isinstance(r, EventRecord)}
            return [self._append_event(records, run_id, "run.finished", {"status": "interrupted"})
                    for run_id, event in last.items() if event.type not in _TERMINAL]

    def context(self, active_run_id: str) -> list[AgentMessage]:
        _id(active_run_id)
        records = self.read()
        completed = {r.event.run_id for r in records if isinstance(r, EventRecord) and r.event.type == "run.completed"}
        messages = [r.message for r in records if isinstance(r, MessageRecord)
                    and (r.run_id is None or r.run_id == active_run_id or r.run_id in completed)]
        self.guard.check(messages)
        to_openai_messages(messages)
        return messages


class SessionManager:
    def __init__(self, root: Path = Path(__file__).resolve().parents[2] / ".data" / "sessions", *, secrets: tuple[str, ...]) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / ".locks").mkdir(exist_ok=True)
        self.secrets = secrets

    def create(self) -> Session:
        session = Session(self.root, uuid4().hex, self.secrets)
        with session.lock:
            header = SessionHeader(version=1, id=session.id, created_at=_now())
            session.guard.check(header.model_dump(mode="json"))
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
        return {session_id: self.open(session_id).recover() for session_id, _ in self.list()}

    def list(self) -> list[tuple[str, str]]:
        result = []
        for path in sorted(self.root.glob("*.jsonl")):
            session = self.open(path.stem)
            titles = [r.title for r in session.read() if isinstance(r, SessionInfoRecord)]
            result.append((session.id, titles[-1] if titles else ""))
        return result
