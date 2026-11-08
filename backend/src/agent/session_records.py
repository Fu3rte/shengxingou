import re
from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter

from .compact import Summary
from .tools.todo import validate_result as validate_todo_result
from ..ai.models.openai import to_openai_messages
from .types import AgentMessage, AssistantMessage, SystemMessage, ToolResultMessage


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
    queue_id: str | None = Field(default=None, min_length=1)


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
        "run.started",
        "model.request",
        "model.delta",
        "model.response",
        "compact.request",
        "compact.completed",
        "tool.started",
        "tool.completed",
        "tool.confirmation_requested",
        "tool.confirmation_resolved",
        "steer.queued",
        "steer.applied",
        "run.completed",
        "run.finished",
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


Record = Annotated[
    SessionHeader | MessageRecord | SessionInfoRecord | EventRecord,
    Field(discriminator="type"),
]
_RECORD = TypeAdapter(Record)
_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
TERMINAL = {"run.completed", "run.finished"}


def validate_id(value: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError("无效 ID")
    return value


def timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _utc(value: str) -> None:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise ValueError("时间必须为 UTC")


def validate_event(
    event: ExecutionEvent, confirmations: dict[tuple[str, int, str], bool]
) -> None:
    data = event.data
    if event.type.startswith(("model.", "tool.")):
        if type(data.get("turn_index")) is not int or data["turn_index"] < 1:
            raise ValueError("无效 Turn 序号")
    if event.type == "run.started" and not isinstance(data.get("input"), str):
        raise ValueError("无效运行输入")
    if event.type == "run.completed" and not isinstance(data.get("answer"), str):
        raise ValueError("无效运行结果")
    if event.type == "run.finished" and data.get("status") not in {
        "stopped",
        "limited",
        "failed",
        "interrupted",
    }:
        raise ValueError("无效运行状态")
    if event.type == "model.request" and (
        set(data) != {"turn_index", "model", "messages", "tools"}
        or not isinstance(data.get("model"), str)
        or not isinstance(data.get("messages"), list)
        or not isinstance(data.get("tools"), list)
    ):
        raise ValueError("无效模型请求")
    if event.type == "compact.request" and (
        set(data) != {"model", "messages", "tools"}
        or not isinstance(data.get("model"), str)
        or not isinstance(data.get("messages"), list)
        or data.get("tools") != []
    ):
        raise ValueError("无效摘要请求")
    if event.type == "compact.completed":
        if set(data) != {"summary", "covered", "previous"}:
            raise ValueError("无效压缩记录")
        Summary.model_validate(data["summary"])
        if (
            not isinstance(data["covered"], list)
            or not data["covered"]
            or any(not isinstance(item, str) for item in data["covered"])
            or (data["previous"] is not None and not isinstance(data["previous"], str))
        ):
            raise ValueError("无效压缩范围")
    if event.type == "model.delta":
        fragments = data.get("fragments")
        if (
            set(data) != {"turn_index", "fragments"}
            or not isinstance(fragments, dict)
            or not fragments
            or any(
                not isinstance(key, str)
                or not isinstance(value, str)
                or (
                    key != "text"
                    and not re.fullmatch(r"tool:\d+:(id|name|arguments)", key)
                )
                for key, value in fragments.items()
            )
        ):
            raise ValueError("无效模型流事件")
    if event.type == "model.response":
        if (
            set(data) != {"turn_index", "message", "finish_reason", "usage"}
            or (
                data["finish_reason"] is not None
                and not isinstance(data["finish_reason"], str)
            )
            or (data["usage"] is not None and not isinstance(data["usage"], dict))
        ):
            raise ValueError("无效模型响应")
        if data["message"] is not None:
            AssistantMessage.model_validate(data["message"])
            if data["finish_reason"] not in {"stop", "tool_calls"}:
                raise ValueError("无效模型响应")
    if event.type == "steer.queued" and (
        set(data) != {"queue_id", "content"}
        or not isinstance(data.get("queue_id"), str)
        or not data["queue_id"]
        or not isinstance(data.get("content"), str)
    ):
        raise ValueError("无效排队消息")
    if event.type == "steer.applied" and (
        set(data) != {"queue_id", "message_id"}
        or not isinstance(data.get("queue_id"), str)
        or not data["queue_id"]
        or not isinstance(data.get("message_id"), str)
        or not data["message_id"]
    ):
        raise ValueError("无效已应用消息")
    if event.type in {"tool.started", "tool.completed"}:
        keys = ("tool_call_id", "name", "arguments")
        if event.type == "tool.completed":
            keys += ("result",)
        if (
            set(data) != {"turn_index", *keys}
            or any(not isinstance(data.get(key), str) for key in keys)
            or not data["tool_call_id"]
            or not data["name"]
        ):
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


def validate_steering(records: list[Record]) -> None:
    queued: dict[tuple[str, str], tuple[str, int]] = {}
    seen: set[tuple[str, str]] = set()
    applied_messages: set[str] = set()
    last_applied_index: dict[str, int] = {}
    messages: dict[str, tuple[MessageRecord, int]] = {}
    for index, record in enumerate(records):
        if isinstance(record, MessageRecord):
            messages[record.id] = (record, index)
            if record.queue_id is not None:
                key = (record.run_id, record.queue_id)
                if (
                    key not in queued
                    or record.message.role != "user"
                    or record.message.content != queued[key][0]
                ):
                    raise ValueError("排队消息关联无效")
        if not isinstance(record, EventRecord):
            continue
        event = record.event
        if event.type == "steer.queued":
            key = (event.run_id, event.data["queue_id"])
            if key in seen:
                raise ValueError("排队消息重复")
            seen.add(key)
            queued[key] = (event.data["content"], index)
        if event.type == "steer.applied":
            key = (event.run_id, event.data["queue_id"])
            entry = messages.get(event.data["message_id"])
            if key not in queued or entry is None:
                raise ValueError("排队消息关联无效")
            if key != next(
                queued_key for queued_key in queued if queued_key[0] == event.run_id
            ):
                raise ValueError("排队消息顺序无效")
            message, message_index = entry
            if message_index <= last_applied_index.get(event.run_id, -1):
                raise ValueError("排队消息顺序无效")
            content, queue_index = queued[key]
            if (
                message.id in applied_messages
                or message.run_id != event.run_id
                or message.queue_id != event.data["queue_id"]
                or message.message.role != "user"
                or message.message.content != content
                or message_index <= queue_index
            ):
                raise ValueError("排队消息关联无效")
            applied_messages.add(message.id)
            last_applied_index[event.run_id] = message_index
            del queued[key]


def validate_tool_results(records: list[Record]) -> None:
    pending: dict[tuple[str, str], str] = {}
    for record in records:
        if isinstance(record, EventRecord) and record.event.type == "tool.completed":
            event = record.event
            key = (event.run_id, event.data["tool_call_id"])
            if key in pending:
                raise ValueError("工具结果重复")
            pending[key] = event.data["result"]
        if isinstance(record, MessageRecord) and isinstance(
            record.message, ToolResultMessage
        ):
            key = (record.run_id, record.message.tool_call_id)
            if key not in pending or pending[key] != record.message.content:
                raise ValueError("工具结果未持久化")
            del pending[key]
        if isinstance(record, EventRecord) and record.event.type == "run.completed":
            if any(run_id == record.event.run_id for run_id, _ in pending):
                raise ValueError("运行缺少工具结果消息")


def validate_todo_results(records: list[Record]) -> None:
    for record in records:
        if not isinstance(record, EventRecord) or record.event.type != "tool.completed":
            continue
        data = record.event.data
        if data.get("name") != "todo":
            continue
        validate_todo_result(data["arguments"], data["result"])


def visible_records(records: list[Record], active_run_id: str) -> list[MessageRecord]:
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
    return [
        r
        for r in records
        if isinstance(r, MessageRecord)
        and (r.run_id is None or r.run_id == active_run_id or r.run_id in completed)
        and (r.queue_id is None or r.id in applied)
    ]


def latest_compaction(records: list[Record], active_run_id: str) -> EventRecord | None:
    completed = {
        r.event.run_id
        for r in records
        if isinstance(r, EventRecord) and r.event.type == "run.completed"
    }
    return next(
        (
            r
            for r in reversed(records)
            if isinstance(r, EventRecord)
            and r.event.type == "compact.completed"
            and (r.event.run_id == active_run_id or r.event.run_id in completed)
        ),
        None,
    )


def validate_compactions(records: list[Record]) -> None:
    pending: set[str] = set()
    for index, record in enumerate(records):
        if not isinstance(record, EventRecord):
            continue
        if record.event.type == "compact.request":
            if record.event.run_id in pending:
                raise ValueError("摘要请求重复")
            pending.add(record.event.run_id)
        if record.event.type != "compact.completed":
            continue
        if record.event.run_id not in pending:
            raise ValueError("摘要请求不存在")
        pending.remove(record.event.run_id)
        prior = records[:index]
        run_id = record.event.run_id
        visible = [
            r
            for r in visible_records(prior, run_id)
            if not isinstance(r.message, SystemMessage)
        ]
        previous = latest_compaction(prior, run_id)
        covered = record.event.data["covered"]
        old = previous.event.data["covered"] if previous else []
        if (
            record.event.data["previous"] != (previous.id if previous else None)
            or covered[: len(old)] != old
            or len(covered) <= len(old)
            or covered != [r.id for r in visible[: len(covered)]]
        ):
            raise ValueError("压缩覆盖范围无效")
        to_openai_messages([r.message for r in visible[: len(covered)]])


def parse_records(data: bytes, session_id: str) -> list[Record]:
    if not data or not data.endswith(b"\n"):
        raise ValueError("会话记录残缺")
    records = [_RECORD.validate_json(line) for line in data.splitlines()]
    if not isinstance(records[0], SessionHeader) or records[0].id != session_id:
        raise ValueError("无效会话头")
    seen: set[str] = set()
    runs: dict[str, tuple[int, bool]] = {}
    requests: dict[str, int] = {}
    confirmations: dict[tuple[str, int, str], bool] = {}
    for index, record in enumerate(records):
        validate_id(record.id)
        if record.id in seen:
            raise ValueError("记录 ID 重复")
        seen.add(record.id)
        _utc(
            record.created_at if isinstance(record, SessionHeader) else record.timestamp
        )
        if index and isinstance(record, SessionHeader):
            raise ValueError("会话头重复")
        if isinstance(record, MessageRecord):
            if record.run_id is None:
                if not isinstance(record.message, SystemMessage):
                    raise ValueError("运行外消息必须是系统消息")
            else:
                validate_id(record.run_id)
                if record.run_id not in runs or runs[record.run_id][1]:
                    raise ValueError("消息所属运行无效")
        if isinstance(record, EventRecord):
            event = record.event
            validate_id(event.run_id)
            _utc(event.time)
            previous, terminal = runs.get(event.run_id, (0, False))
            if (
                terminal
                or event.seq != previous + 1
                or ((previous == 0) != (event.type == "run.started"))
            ):
                raise ValueError("运行事件顺序无效")
            if previous == 0 and any(not ended for _, ended in runs.values()):
                raise ValueError("会话已有运行")
            validate_event(event, confirmations)
            if event.type in {"model.request", "compact.request"}:
                requests[event.run_id] = requests.get(event.run_id, 0) + 1
                if requests[event.run_id] > 64:
                    raise ValueError("模型请求次数达到上限")
            runs[event.run_id] = (event.seq, event.type in TERMINAL)
    validate_steering(records)
    validate_tool_results(records)
    validate_todo_results(records)
    validate_compactions(records)
    return records
