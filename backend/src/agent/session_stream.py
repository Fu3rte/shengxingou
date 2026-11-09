from collections.abc import AsyncIterable, AsyncIterator
from dataclasses import dataclass
from uuid import uuid4

from filelock import AsyncFileLock
from openai.types.chat import ChatCompletionChunk

from .session import Session, append_model_record, read, run_sync
from .session_records import EventRecord, ExecutionEvent, TERMINAL, timestamp
from ..ai.models.normalized import async_normalized_stream


@dataclass
class _ModelStreamState:
    ids: set[str]
    seq: int
    fingerprint: tuple[int, int, int, int]


def _fingerprint(session: Session) -> tuple[int, int, int, int]:
    stat = session.path.stat()
    return stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino


def _begin_model_stream(
    session: Session, run_id: str, turn_index: int
) -> _ModelStreamState:
    with session.lock:
        records = read(session)
        events = [
            r.event
            for r in records
            if isinstance(r, EventRecord) and r.event.run_id == run_id
        ]
        if (
            not events
            or events[-1].type in TERMINAL
            or not any(
                event.type == "model.request"
                and event.data.get("turn_index") == turn_index
                for event in events
            )
            or any(
                event.type in {"model.delta", "model.response"}
                and event.data.get("turn_index") == turn_index
                for event in events
            )
        ):
            raise ValueError("模型请求状态无效")
        return _ModelStreamState(
            {record.id for record in records}, events[-1].seq, _fingerprint(session)
        )


def _append_model_stream(
    session: Session,
    run_id: str,
    turn_index: int,
    state: _ModelStreamState,
    normalized: dict,
) -> ExecutionEvent:
    with session.lock:
        if _fingerprint(session) != state.fingerprint:
            records = read(session)
            events = [
                r.event
                for r in records
                if isinstance(r, EventRecord) and r.event.run_id == run_id
            ]
            if not events or events[-1].type in TERMINAL:
                raise ValueError("运行已经结束")
            state.ids = {record.id for record in records}
            state.seq = events[-1].seq
        now = timestamp()
        data = {"turn_index": turn_index, **normalized["data"]}
        event = ExecutionEvent(
            run_id=run_id,
            seq=state.seq + 1,
            time=now,
            type=normalized["type"],
            data=data,
        )
        record = EventRecord(id=uuid4().hex, timestamp=now, event=event)
        append_model_record(session, record, ids=state.ids)
        state.ids.add(record.id)
        state.seq = event.seq
        state.fingerprint = _fingerprint(session)
        return event


async def record_model_stream_async(
    session: Session,
    run_id: str,
    turn_index: int,
    chunks: AsyncIterable[ChatCompletionChunk],
) -> AsyncIterator[ExecutionEvent]:
    if type(turn_index) is not int or turn_index < 1:
        raise ValueError("无效 Turn 序号")
    async with AsyncFileLock(session.stream_lock.lock_file):
        state = await run_sync(_begin_model_stream, session, run_id, turn_index)
        async for normalized in async_normalized_stream(chunks, session.guard.secrets):
            yield await run_sync(
                _append_model_stream, session, run_id, turn_index, state, normalized
            )
