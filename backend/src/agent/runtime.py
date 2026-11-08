import asyncio
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from threading import Lock
from uuid import uuid4

from openai import AsyncOpenAI

from .agent_loop import run
from .security import SecretGuard, check, create_guard
from .session import (
    Session,
    _LIVE_RUNS,
    _LIVE_RUNS_LOCK,
    await_task,
    release_run,
    run_sync,
)
from .session_records import ExecutionEvent
from .tools.registry import ToolRegistry, create_registry
from .types import UserMessage
from ..ai.models.openai import reject_cookies, validate_client


@dataclass
class SessionState:
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)
    control: Lock = field(default_factory=Lock)
    active: str | None = None
    stop: bool = False
    shutdown: bool = False
    confirmation_failed: bool = False
    queue: deque[tuple[str, str]] = field(default_factory=deque)
    confirmation: tuple[str, int, str] | None = None
    decision: str | None = None
    guard: SecretGuard | None = None
    tasks: dict[str, asyncio.Task] = field(default_factory=dict)
    finished: set[str] = field(default_factory=set)
    model_task: asyncio.Task | None = None


@dataclass
class Agent:
    session: Session
    source_client: AsyncOpenAI
    client: AsyncOpenAI
    credentials: tuple[str, ...] = field(repr=False)
    model: str
    registry: ToolRegistry
    state: SessionState


_states_lock = Lock()
_states: dict[str, SessionState] = {}


class InvalidConfirmation(ValueError):
    pass


async def create_agent(session: Session, client: AsyncOpenAI, model: str) -> Agent:
    if not model:
        raise ValueError("模型不能为空")
    credentials = validate_client(client)
    client._client.cookies.jar.set_policy(reject_cookies())
    with _states_lock:
        state = _states.setdefault(str(session.path), SessionState())
    async with state.condition:
        guard = create_guard((*session.guard.secrets, *credentials))
        if state.active is not None:
            if state.guard is None or not set(guard.secrets).issubset(
                state.guard.secrets
            ):
                raise ValueError("运行期间不能更改密钥")
            session.guard = state.guard
        else:
            session.guard = guard
        await run_sync(session.recover)
    registry = create_registry(
        None, session.todo_snapshot, todo_execute=session.execute_todo
    )
    return Agent(session, client, client.copy(), credentials, model, registry, state)


def _start_run(session: Session, run_id: str, content: str) -> None:
    session.run_lock.acquire(blocking=False)
    key = (str(session.path), run_id)
    started = False
    submitted = False
    try:
        session.recover()
        with session.live_lock, _LIVE_RUNS_LOCK:
            _LIVE_RUNS.add(key)
        session.append_event(run_id, "run.started", {"input": content})
        started = True
        session.append_message(UserMessage(content=content), run_id)
        submitted = True
    finally:
        if not submitted:
            try:
                if started:
                    session.append_event(run_id, "run.finished", {"status": "failed"})
            finally:
                with session.live_lock, _LIVE_RUNS_LOCK:
                    _LIVE_RUNS.discard(key)
                session.run_lock.release()


async def protect(coroutine):
    return await await_task(asyncio.create_task(coroutine))


async def append_message(agent: Agent, content: str) -> str:
    return await protect(_append_message(agent, content))


async def _append_message(agent: Agent, content: str) -> str:
    if (
        validate_client(agent.source_client) != agent.credentials
        or validate_client(agent.client) != agent.credentials
    ):
        raise ValueError("运行期间不能更改密钥")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("消息不能为空")
    check(agent.session.guard, content)
    state = agent.state
    async with state.condition:
        while state.active is not None:
            run_id = state.active
            events = await run_sync(agent.session.trace, run_id)
            if events and events[-1].type in {"run.completed", "run.finished"}:
                await state.condition.wait_for(lambda: state.active != run_id)
                continue
            if state.guard is None:
                raise ValueError("运行安全状态无效")
            check(state.guard, content)
            queue_id = uuid4().hex
            await run_sync(
                agent.session.append_event,
                run_id,
                "steer.queued",
                {"queue_id": queue_id, "content": content},
            )
            state.queue.append((queue_id, content))
            state.condition.notify_all()
            return run_id
        run_id = uuid4().hex
        await run_sync(_start_run, agent.session, run_id, content)
        state.active = run_id
        with state.control:
            state.stop = False
            state.shutdown = False
        state.confirmation_failed = False
        state.queue.clear()
        state.confirmation = None
        state.decision = None
        state.guard = agent.session.guard
        submitted = False
        try:
            state.tasks[run_id] = asyncio.create_task(run(agent, run_id))
            submitted = True
            state.tasks[run_id].add_done_callback(
                lambda task: task.exception() if not task.cancelled() else None
            )
        finally:
            if not submitted:
                try:
                    await run_sync(
                        agent.session.append_event,
                        run_id,
                        "run.finished",
                        {"status": "failed"},
                    )
                finally:
                    state.active = None
                    state.finished.add(run_id)
                    await run_sync(release_run, agent.session, run_id)
                    state.condition.notify_all()
        state.condition.notify_all()
        return run_id


def is_active(agent: Agent, run_id: str | None = None) -> bool:
    return agent.state.active is not None and (
        run_id is None or agent.state.active == run_id
    )


async def abort(agent: Agent, run_id: str) -> bool:
    state = agent.state
    async with state.condition:
        events = await run_sync(agent.session.trace, run_id)
        if not events:
            raise ValueError("运行不存在")
        if events[-1].type in {"run.completed", "run.finished"}:
            return False
        if state.active != run_id:
            raise ValueError("运行不在执行中")
        with state.control:
            state.stop = True
        state.condition.notify_all()
        return True


async def confirm(
    agent: Agent, run_id: str, turn_index: int, tool_call_id: str, decision: str
) -> None:
    await protect(_confirm(agent, run_id, turn_index, tool_call_id, decision))


async def _confirm(
    agent: Agent, run_id: str, turn_index: int, tool_call_id: str, decision: str
) -> None:
    state = agent.state
    async with state.condition:
        if decision not in {"approved", "rejected"}:
            raise InvalidConfirmation("无效确认选择")
        if (
            state.active != run_id
            or state.stop
            or state.shutdown
            or state.confirmation != (run_id, turn_index, tool_call_id)
            or state.decision is not None
        ):
            raise InvalidConfirmation("确认请求无效")
        resolved = False
        try:
            await run_sync(
                agent.session.append_event,
                run_id,
                "tool.confirmation_resolved",
                {
                    "turn_index": turn_index,
                    "tool_call_id": tool_call_id,
                    "decision": decision,
                },
            )
            state.decision = decision
            resolved = True
        finally:
            if not resolved:
                state.confirmation_failed = True
            state.condition.notify_all()


async def close(agent: Agent, timeout: float | None = None) -> None:
    await protect(_close(agent, timeout))


async def _close(agent: Agent, timeout: float | None = None) -> None:
    state = agent.state
    async with state.condition:
        with state.control:
            state.shutdown = True
        run_id = state.active
        task = state.tasks.get(run_id) if run_id is not None else None
        if state.model_task is not None and not state.model_task.cancelling():
            state.model_task.cancel()
        state.condition.notify_all()
    if task is not None:
        await asyncio.wait_for(asyncio.shield(task), timeout)


async def wait(agent: Agent, run_id: str, timeout: float | None = None) -> None:
    async with agent.state.condition:
        task = agent.state.tasks.get(run_id)
        if task is None:
            raise ValueError("运行不存在")
    await asyncio.wait_for(asyncio.shield(task), timeout)


async def trace(agent: Agent, run_id: str) -> list[ExecutionEvent]:
    return await run_sync(agent.session.trace, run_id)


async def _notify(state: SessionState) -> None:
    async with state.condition:
        state.condition.notify_all()


async def events(
    agent: Agent, run_id: str, after: int = 0
) -> AsyncIterator[ExecutionEvent]:
    if type(after) is not int or after < 0:
        raise ValueError("无效事件游标")
    state = agent.state
    cursor = after
    while True:
        async with state.condition:
            snapshot = await run_sync(agent.session.trace, run_id)
            if not snapshot or cursor > snapshot[-1].seq:
                raise ValueError("无效事件游标")
            pending = [event for event in snapshot if event.seq > cursor]
            if not pending:
                if snapshot[-1].type in {"run.completed", "run.finished"}:
                    return
                if run_id in state.finished:
                    raise RuntimeError("运行终态未持久化")
                timer = asyncio.get_running_loop().call_later(
                    1, lambda: asyncio.create_task(_notify(state))
                )
                try:
                    await state.condition.wait()
                finally:
                    timer.cancel()
                continue
        for event in pending:
            cursor = event.seq
            yield event
            if event.type in {"run.completed", "run.finished"}:
                return
