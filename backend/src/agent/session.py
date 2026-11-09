import asyncio
import json
import os
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from uuid import uuid4

from filelock import FileLock
from pydantic import JsonValue, TypeAdapter

from .compact import estimate_tokens, size, summary_message
from .security import SecretGuard, check, create_guard
from .tools import registry as tool_registry, todo
from .tools.registry import ToolRegistry
from .tools.todo import validate_delete_confirmation
from .types import (
    AgentMessage,
    AssistantMessage,
    CustomMessage,
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
    validate_compactions as _validate_compactions,
    visible_records,
    latest_compaction,
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


@dataclass(eq=False)
class Session:
    root: Path
    id: str
    guard: SecretGuard
    path: Path
    lock: FileLock
    stream_lock: FileLock
    tool_lock: FileLock
    todo_lock: FileLock
    todo_path: Path
    run_lock: FileLock
    live_lock: RLock


def _build_session(root: Path, session_id: str, secrets: tuple[str, ...]) -> Session:
    root = root.resolve()
    session_id = _id(session_id)
    guard = create_guard(secrets)
    path = root / f"{session_id}.jsonl"
    if path.resolve().parent != root or path.is_symlink():
        raise ValueError("无效会话路径")
    lock = FileLock(str(root / ".locks" / f"{session_id}.lock"))
    stream_lock = FileLock(str(root / ".locks" / f"{session_id}.stream.lock"))
    tool_lock = FileLock(str(root / ".locks" / f"{session_id}.tool.lock"))
    todo_lock = FileLock(str(root / ".locks" / f"{session_id}.todo.lock"))
    todo_path = root / ".todo" / f"session-{session_id}.jsonl"
    run_lock = FileLock(
        str(root / ".locks" / f"{session_id}.run.lock"), thread_local=False
    )
    with _LIVE_RUNS_LOCK:
        if str(path) not in _LIVE_LOCKS:
            _LIVE_LOCKS[str(path)] = RLock()
        live_lock = _LIVE_LOCKS[str(path)]
    return Session(
        root,
        session_id,
        guard,
        path,
        lock,
        stream_lock,
        tool_lock,
        todo_lock,
        todo_path,
        run_lock,
        live_lock,
    )


def release_run(session: Session, run_id: str) -> None:
    try:
        with session.live_lock, _LIVE_RUNS_LOCK:
            _LIVE_RUNS.discard((str(session.path), run_id))
    finally:
        session.run_lock.release()


def _load_records(session: Session) -> list[Record]:
    if str(session.path) in _WRITE_FAILURES:
        raise OSError("会话写入状态不确定")
    return parse_records(session.path.read_bytes(), session.id)


def read(session: Session) -> list[Record]:
    with session.lock:
        return _load_records(session)


def _write_record(
    session: Session, record: Record, *, ids: set[str] | None = None
) -> None:
    if str(session.path) in _WRITE_FAILURES:
        raise OSError("会话写入状态不确定")
    if ids is None:
        ids = {existing.id for existing in _load_records(session)}
    if record.id in ids:
        raise ValueError("记录 ID 重复")
    value = record.model_dump(mode="json")
    if isinstance(record, MessageRecord) and record.queue_id is None:
        del value["queue_id"]
    check(session.guard, value)
    file = session.path.open("ab")
    committed = False
    try:
        with file:
            file.write(
                (json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n").encode(
                    "utf-8"
                )
            )
            file.flush()
            os.fsync(file.fileno())
        committed = True
    finally:
        if not committed:
            _WRITE_FAILURES.add(str(session.path))


def append_model_record(
    session: Session, record: EventRecord, *, ids: set[str]
) -> None:
    with session.lock:
        if record.event.type not in {"model.delta", "model.response"}:
            raise ValueError("模型流记录类型无效")
        _validate_event(record.event, {})
        _write_record(session, record, ids=ids)


def set_title(session: Session, title: str) -> SessionInfoRecord:
    check(session.guard, title)
    with session.lock:
        _load_records(session)
        record = SessionInfoRecord(id=uuid4().hex, timestamp=_now(), title=title)
        _write_record(session, record)
        return record


def append_message(
    session: Session,
    message: AgentMessage,
    run_id: str | None,
    *,
    queue_id: str | None = None,
) -> MessageRecord:
    check(session.guard, message)
    if isinstance(message, AssistantMessage):
        for block in message.content:
            if isinstance(block, ToolCallContent):
                check(
                    session.guard, TypeAdapter(JsonValue).validate_json(block.arguments)
                )
    with session.lock:
        records = _load_records(session)
        if run_id is None:
            if not isinstance(message, SystemMessage):
                raise ValueError("运行外消息必须是系统消息")
        else:
            _id(run_id)
            _active_events(records, run_id)
        record = MessageRecord(
            id=uuid4().hex,
            timestamp=_now(),
            run_id=run_id,
            message=message,
            queue_id=queue_id,
        )
        _validate_steering([*records, record])
        _validate_tool_results([*records, record])
        _write_record(session, record)
        return record


def record_event(
    session: Session, run_id: str, event_type: str, data: dict[str, JsonValue]
) -> ExecutionEvent:
    check(session.guard, data)
    if event_type in {"model.delta", "model.response"}:
        raise ValueError("模型流事件须经过跨片段密钥检查")
    if event_type in {"tool.started", "tool.completed"} and isinstance(
        data.get("arguments"), str
    ):
        check(session.guard, TypeAdapter(JsonValue).validate_json(data["arguments"]))
    if event_type == "tool.completed" and data.get("name") == "todo":
        raise ValueError("待办事件须通过待办接口提交")
    with session.lock:
        records = _load_records(session)
        event = _commit_event(session, records, run_id, event_type, data)
        if (
            event_type == "tool.confirmation_resolved"
            and data.get("decision") == "rejected"
        ):
            _commit_event(
                session,
                _load_records(session),
                run_id,
                "run.finished",
                {"status": "stopped"},
            )
        return event


def _commit_event(
    session: Session,
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
        last = {r.event.run_id: r.event for r in records if isinstance(r, EventRecord)}
        if any(event.type not in _TERMINAL for event in last.values()):
            raise ValueError("会话已有运行")
    if (
        event_type in {"model.request", "compact.request"}
        and sum(event.type in {"model.request", "compact.request"} for event in events)
        >= 64
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
    _validate_compactions([*records, record])
    _write_record(session, record, ids={existing.id for existing in records})
    return event


def _active_events(records: list[Record], run_id: str) -> list[ExecutionEvent]:
    events = [
        r.event
        for r in records
        if isinstance(r, EventRecord) and r.event.run_id == run_id
    ]
    if not events or events[-1].type in _TERMINAL:
        raise ValueError("运行已经结束")
    return events


def process_tool_call(
    session: Session,
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
    parameters = tool_registry.parse_arguments(
        registry, name, arguments, secrets=session.guard.secrets
    )
    with session.tool_lock, session.todo_lock if name == "todo" else nullcontext():
        with session.lock:
            events = _active_events(_load_records(session), run_id)
            calls = [
                event
                for event in events
                if event.data.get("turn_index") == turn_index
                and event.data.get("tool_call_id") == tool_call_id
            ]
            if not any(
                event.type == "tool.started"
                and event.data.get("name") == name
                and event.data.get("arguments") == arguments
                for event in calls
            ):
                raise ValueError("工具尚未启动")
            if any(event.type == "tool.completed" for event in calls):
                raise ValueError("工具调用已经完成")
            if name == "todo":
                items, next_id = todo.load_shared(session.todo_path)
                validate_delete_confirmation(
                    parameters,
                    items,
                    [e.data for e in calls if e.type == "tool.confirmation_requested"],
                    [e.data for e in calls if e.type == "tool.confirmation_resolved"],
                )
        if name == "todo":
            result, store = todo.stage_change(parameters, items, next_id, start=start)
        else:
            result = tool_registry.invoke_handler(
                tool_registry.get(registry, name),
                parameters,
                secrets=session.guard.secrets,
                start=start,
            )
        if result is None:
            return None
        check(session.guard, result)
        with session.lock:
            records = _load_records(session)
            # 工具执行期间运行可能结束，提交前重新校验。
            _active_events(records, run_id)
            if name == "todo":
                todo.commit_shared(session.todo_path, parameters, store, next_id)
            _commit_event(
                session,
                records,
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


def todo_snapshot(session: Session) -> list[dict]:
    return todo.shared_snapshot(session.todo_path, session.todo_lock)


def trace(session: Session, run_id: str) -> list[ExecutionEvent]:
    _id(run_id)
    return [
        r.event
        for r in read(session)
        if isinstance(r, EventRecord) and r.event.run_id == run_id
    ]


def recover(session: Session) -> list[ExecutionEvent]:
    with _LIVE_RUNS_LOCK:
        if any(path == str(session.path) for path, _ in _LIVE_RUNS):
            return []
    with session.run_lock.acquire(blocking=False), session.lock, session.live_lock:
        with _LIVE_RUNS_LOCK:
            if any(path == str(session.path) for path, _ in _LIVE_RUNS):
                return []
        records = _load_records(session)
        last = {r.event.run_id: r.event for r in records if isinstance(r, EventRecord)}
        return [
            _commit_event(
                session, records, run_id, "run.finished", {"status": "interrupted"}
            )
            for run_id, event in last.items()
            if event.type not in _TERMINAL
        ]


def context_parts(session: Session, active_run_id: str):
    _id(active_run_id)
    records = read(session)
    visible = visible_records(records, active_run_id)
    compact = latest_compaction(records, active_run_id)
    covered = set(compact.event.data["covered"]) if compact else set()
    systems = [r.message for r in visible if isinstance(r.message, SystemMessage)]
    recent = [
        r
        for r in visible
        if not isinstance(r.message, SystemMessage) and r.id not in covered
    ]
    return systems, compact, recent


def context_tokens(
    session: Session, active_run_id: str, model: str, messages: list, tools: list
) -> float:
    records = read(session)
    visible = visible_records(records, active_run_id)
    compact = latest_compaction(records, active_run_id)
    compact_index = records.index(compact) if compact else -1
    covered = set(compact.event.data["covered"]) if compact else set()
    systems = [r for r in visible if isinstance(r.message, SystemMessage)]
    recent = [
        r
        for r in visible
        if not isinstance(r.message, (SystemMessage, CustomMessage))
        and r.id not in covered
    ]
    positions = {r.id: i for i, r in enumerate(systems)}
    positions.update({
        r.id: i + len(systems) + bool(compact) for i, r in enumerate(recent)
    })
    requests = {}
    responses = {}
    baseline = None
    for index, record in enumerate(records):
        if isinstance(record, EventRecord):
            event = record.event
            key = (event.run_id, event.data.get("turn_index"))
            if event.type == "model.request":
                requests[key] = event.data
                responses.pop(event.run_id, None)
            elif event.type == "model.response":
                responses[event.run_id] = (index, requests.get(key), event.data)
        elif isinstance(record, MessageRecord) and record.run_id in responses:
            response_index, request, response = responses.pop(record.run_id)
            usage = response.get("usage")
            total = usage.get("total_tokens") if isinstance(usage, dict) else None
            if (
                record.id in positions
                and response_index > compact_index
                and isinstance(record.message, AssistantMessage)
                and record.message.model_dump(mode="json") == response.get("message")
                and type(total) is int
                and total > 0
            ):
                baseline = (positions[record.id], request, total)
    if baseline:
        position, request, total = baseline
        if (
            request is not None
            and request["model"] == model
            and request["tools"] == tools
            and request["messages"] == messages[:position]
        ):
            return total + (size(messages[position + 1 :]) - size([])) / 4
    return estimate_tokens(messages, tools)


def context(session: Session, active_run_id: str) -> list[AgentMessage]:
    systems, compact, recent = context_parts(session, active_run_id)
    messages = [*systems]
    if compact:
        messages.append(summary_message(compact.event.data["summary"]))
    messages.extend(r.message for r in recent)
    check(session.guard, messages)
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


def _session_root(root: Path) -> Path:
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    (root / ".locks").mkdir(exist_ok=True)
    return root


def create_session(root: Path, *, secrets: tuple[str, ...]) -> Session:
    session = _build_session(_session_root(root), uuid4().hex, secrets)
    with session.lock:
        header = SessionHeader(version=1, id=session.id, created_at=_now())
        check(session.guard, header.model_dump(mode="json"))
        with session.path.open("xb") as file:
            file.write((header.model_dump_json() + "\n").encode("utf-8"))
            file.flush()
            os.fsync(file.fileno())
    return session


def open_session(root: Path, session_id: str, *, secrets: tuple[str, ...]) -> Session:
    session = _build_session(_session_root(root), session_id, secrets)
    read(session)
    return session


def recover_sessions(
    root: Path, *, secrets: tuple[str, ...]
) -> dict[str, list[ExecutionEvent]]:
    return {
        session_id: recover(open_session(root, session_id, secrets=secrets))
        for session_id, _ in list_sessions(root, secrets=secrets)
    }


def list_sessions(root: Path, *, secrets: tuple[str, ...]) -> list[tuple[str, str]]:
    result = []
    for path in sorted(_session_root(root).glob("*.jsonl")):
        session = open_session(root, path.stem, secrets=secrets)
        titles = [r.title for r in read(session) if isinstance(r, SessionInfoRecord)]
        result.append((session.id, titles[-1] if titles else ""))
    return result
