import asyncio
import logging
import sys
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from http.cookiejar import DefaultCookiePolicy
from threading import Condition, Event, Lock, RLock, Thread, _register_atexit, current_thread
from uuid import uuid4

import httpcore2
import httpx2
from openai import AsyncOpenAI
from pydantic import JsonValue, TypeAdapter

from .security import SecretGuard
from .session import ExecutionEvent, Session, _LIVE_RUNS, _LIVE_RUNS_LOCK
from .tools.registry import create_full_registry
from .tools.todo import TodoCommand, TodoList
from .types import AssistantMessage, TextContent, ToolCallContent, ToolResultMessage, UserMessage
from ..ai.models.openai import to_openai_messages


class _SessionState:
    def __init__(self) -> None:
        self.condition = Condition(RLock())
        self.active: str | None = None
        self.stop = False
        self.shutdown = False
        self.confirmation_failed = False
        self.queue: deque[tuple[str, str]] = deque()
        self.confirmation: tuple[str, int, str] | None = None
        self.decision: str | None = None
        self.guard: SecretGuard | None = None
        self.futures: dict[str, Future] = {}
        self.finished: set[str] = set()
        self.executor: ThreadPoolExecutor | None = None
        self.model_stream: _ModelStream | None = None


_states_lock = Lock()
_states: dict[str, _SessionState] = {}
_model_loop_lock = Lock()
_model_loop: asyncio.AbstractEventLoop | None = None
_model_thread: Thread | None = None
_STREAM_END = object()
_STREAM_INTERRUPTED = object()


def _event_loop() -> asyncio.AbstractEventLoop:
    global _model_loop, _model_thread
    with _model_loop_lock:
        if _model_loop is None:
            _model_loop = asyncio.new_event_loop()
            _model_thread = Thread(target=_model_loop.run_forever, name="agent-model-loop", daemon=True)
            _model_thread.start()
        return _model_loop


def _check_control_thread() -> None:
    if current_thread() is _model_thread:
        raise RuntimeError("不能在模型事件循环线程调用同步接口")


def close_model_client(client: AsyncOpenAI) -> None:
    _check_control_thread()
    asyncio.run_coroutine_threadsafe(client.close(), _event_loop()).result()


class _RejectCookies(DefaultCookiePolicy):
    def set_ok(self, cookie, request) -> bool:
        return False

    def return_ok(self, cookie, request) -> bool:
        return False


def _credentials(client: AsyncOpenAI) -> tuple[str, ...]:
    values = (client.api_key, client.admin_api_key, client.webhook_secret)
    transports = (client._client._transport, *client._client._mounts.values())
    if (not isinstance(client.api_key, str) or not client.api_key
            or any(value is not None and not isinstance(value, str) for value in values)
            or client._api_key_provider is not None or client._provider_runtime is not None
            or client.workload_identity is not None or client._client.auth is not None
            or client.base_url.userinfo or client._client.base_url.userinfo
            or client.base_url.query or client._client.base_url.query or client.default_query or client._client.params
            or client._client.cookies or any(client._client.event_hooks.values())
            or any(name.lower() in {"authorization", "proxy-authorization", "api-key", "x-api-key", "cookie"}
                   for name in (*client.default_headers, *client._client.headers))):
        raise ValueError("模型客户端必须使用固定 API key")
    for transport in transports:
        if transport is None:
            continue
        if type(transport) is not httpx2.AsyncHTTPTransport:
            raise ValueError("不支持自定义 HTTP transport")
        pool = transport._pool
        if type(pool) not in {httpcore2.AsyncConnectionPool, httpcore2.AsyncHTTPProxy}:
            raise ValueError("不支持该代理类型")
        if type(pool) is httpcore2.AsyncHTTPProxy and pool._proxy_headers:
            raise ValueError("不支持代理认证")
    return tuple(value for value in values if value)


class _ModelStream:
    def __init__(self, client: AsyncOpenAI, credentials: tuple[str, ...], state: _SessionState, model: str, messages: list, tools: list) -> None:
        self.loop = _event_loop()
        self.queue = asyncio.Queue(maxsize=1)
        self.ready: Future[bool] = Future()
        self.done = Event()
        self.cancelled = Event()
        self.cancel_lock = Lock()
        self.future: Future | None = None
        with state.condition:
            if state.stop or state.shutdown:
                self.ready.set_result(False)
                self.done.set()
            else:
                self.future = asyncio.run_coroutine_threadsafe(self._produce(client, credentials, model, messages, tools), self.loop)
                self.ready.result()

    async def _produce(self, client: AsyncOpenAI, credentials: tuple[str, ...], model: str, messages: list, tools: list) -> None:
        request = None
        stream = None
        try:
            if _credentials(client) != credentials:
                raise ValueError("运行期间不能更改密钥")
            request = asyncio.create_task(client.chat.completions.create(
                model=model, messages=messages, tools=tools, n=1, stream=True,
                stream_options={"include_usage": True},
            ))
            await asyncio.sleep(0)
            self.ready.set_result(True)
            stream = await request
            async for chunk in stream:
                if _credentials(client) != credentials:
                    raise ValueError("运行期间不能更改密钥")
                await self.queue.put(chunk)
        finally:
            try:
                if request is not None and stream is None:
                    if not request.done():
                        request.cancel()
                    completed = self.loop.create_future()
                    def finish_request(_):
                        if not completed.done():
                            completed.set_result(None)
                    request.add_done_callback(finish_request)
                    await completed
                    if not request.cancelled() and request.exception() is None:
                        stream = request.result()
                if stream is not None:
                    await stream.close()
            finally:
                try:
                    if not self.ready.done():
                        error = sys.exception()
                        if error is not None:
                            self.ready.set_exception(error)
                        else:
                            self.ready.set_result(False)
                    if not self.cancelled.is_set():
                        await self.queue.put(_STREAM_END)
                finally:
                    self.done.set()

    def __iter__(self):
        while True:
            item = asyncio.run_coroutine_threadsafe(self.queue.get(), self.loop).result()
            if item is _STREAM_INTERRUPTED:
                raise asyncio.CancelledError()
            if item is _STREAM_END:
                if self.future is not None:
                    self.future.result()
                return
            yield item

    def interrupt(self) -> None:
        with self.cancel_lock:
            if self.cancelled.is_set():
                return
            self.cancelled.set()
            if self.future is not None and not self.done.is_set():
                self.future.cancel()
            asyncio.run_coroutine_threadsafe(self.queue.put(_STREAM_INTERRUPTED), self.loop)

    def close(self) -> None:
        with self.cancel_lock:
            self.cancelled.set()
            if self.future is not None and not self.done.is_set():
                self.future.cancel()
        self.done.wait()


def _shutdown() -> None:
    with _states_lock:
        states = list(_states.values())
    for state in states:
        with state.condition:
            state.shutdown = True
            if state.model_stream is not None:
                state.model_stream.interrupt()
            state.condition.notify_all()


_register_atexit(_shutdown)


class AgentLoop:
    def __init__(self, session: Session, client: AsyncOpenAI, model: str) -> None:
        _check_control_thread()
        if not model:
            raise ValueError("模型不能为空")
        self.credentials = _credentials(client)
        client._client.cookies.jar.set_policy(_RejectCookies())
        self.session = session
        self.source_client = client
        self.client = client.copy()
        self.model = model
        self.registry = create_full_registry(TodoList())
        with _states_lock:
            if str(session.path) not in _states:
                _states[str(session.path)] = _SessionState()
            self.state = _states[str(session.path)]
        with self.state.condition:
            guard = SecretGuard((*session.guard.secrets, *self.credentials))
            if self.state.active is not None:
                if self.state.guard is None or not set(guard.secrets).issubset(self.state.guard.secrets):
                    raise ValueError("运行期间不能更改密钥")
                self.session.guard = self.state.guard
            else:
                self.session.guard = guard
            self.session.recover()

    def append_message(self, content: str) -> str:
        _check_control_thread()
        if _credentials(self.source_client) != self.credentials or _credentials(self.client) != self.credentials:
            raise ValueError("运行期间不能更改密钥")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("消息不能为空")
        self.session.guard.check(content)
        state = self.state
        with state.condition:
            while state.active is not None:
                run_id = state.active
                events = self.session.trace(run_id)
                if events and events[-1].type in {"run.completed", "run.finished"}:
                    state.condition.wait_for(lambda: state.active != run_id)
                    continue
                if state.guard is None:
                    raise ValueError("运行安全状态无效")
                state.guard.check(content)
                queue_id = uuid4().hex
                self.session.append_event(run_id, "steer.queued", {"queue_id": queue_id, "content": content})
                state.queue.append((queue_id, content))
                state.condition.notify_all()
                return run_id
            self.session.run_lock.acquire(blocking=False)
            run_id = uuid4().hex
            key = (str(self.session.path), run_id)
            started = False
            submitted = False
            try:
                self.session.recover()
                state.active = run_id
                state.stop = False
                state.shutdown = False
                state.confirmation_failed = False
                state.queue.clear()
                state.confirmation = None
                state.decision = None
                state.guard = self.session.guard
                with self.session.live_lock, _LIVE_RUNS_LOCK:
                    _LIVE_RUNS.add(key)
                self.session.append_event(run_id, "run.started", {"input": content})
                started = True
                self.session.append_message(UserMessage(content=content), run_id)
                state.executor = ThreadPoolExecutor(max_workers=1)
                state.futures[run_id] = state.executor.submit(self._run, run_id)
                submitted = True
            finally:
                if not submitted:
                    try:
                        if started:
                            self.session.append_event(run_id, "run.finished", {"status": "failed"})
                    finally:
                        state.active = None
                        state.finished.add(run_id)
                        if state.executor is not None:
                            state.executor.shutdown(wait=False)
                            state.executor = None
                        with self.session.live_lock, _LIVE_RUNS_LOCK:
                            _LIVE_RUNS.discard(key)
                        self.session.run_lock.release()
                        state.condition.notify_all()
            state.condition.notify_all()
            return run_id

    def abort(self, run_id: str) -> bool:
        _check_control_thread()
        state = self.state
        with state.condition:
            events = self.session.trace(run_id)
            if not events:
                raise ValueError("运行不存在")
            if events[-1].type in {"run.completed", "run.finished"}:
                return False
            if state.active != run_id:
                raise ValueError("运行不在执行中")
            state.stop = True
            state.condition.notify_all()
            return True

    def confirm(self, run_id: str, turn_index: int, tool_call_id: str, decision: str) -> None:
        _check_control_thread()
        state = self.state
        with state.condition:
            if decision not in {"approved", "rejected"}:
                raise ValueError("无效确认选择")
            if (state.active != run_id or state.stop or state.shutdown
                    or state.confirmation != (run_id, turn_index, tool_call_id) or state.decision is not None):
                raise ValueError("确认请求无效")
            resolved = False
            try:
                self.session.append_event(run_id, "tool.confirmation_resolved", {
                    "turn_index": turn_index, "tool_call_id": tool_call_id, "decision": decision,
                })
                state.decision = decision
                resolved = True
            finally:
                if not resolved:
                    state.confirmation_failed = True
                state.condition.notify_all()

    def close(self, timeout: float | None = None) -> None:
        _check_control_thread()
        with self.state.condition:
            self.state.shutdown = True
            run_id = self.state.active
            future = self.state.futures.get(run_id) if run_id is not None else None
            if self.state.model_stream is not None:
                self.state.model_stream.interrupt()
            self.state.condition.notify_all()
            if not self.state.condition.wait_for(lambda: run_id is None or self.state.active != run_id, timeout=timeout):
                raise TimeoutError("运行关闭超时")
        if future is not None:
            error = future.exception(timeout=timeout)
            if error is not None and not isinstance(error, asyncio.CancelledError):
                future.result()

    def wait(self, run_id: str, timeout: float | None = None) -> None:
        _check_control_thread()
        with self.state.condition:
            future = self.state.futures.get(run_id)
            if future is None:
                raise ValueError("运行不存在")
        future.result(timeout=timeout)

    def trace(self, run_id: str) -> list[ExecutionEvent]:
        _check_control_thread()
        return self.session.trace(run_id)

    def events(self, run_id: str, after: int = 0):
        _check_control_thread()
        if type(after) is not int or after < 0:
            raise ValueError("无效事件游标")
        state = self.state
        cursor = after
        while True:
            with state.condition:
                events = self.session.trace(run_id)
                if not events or cursor > events[-1].seq:
                    raise ValueError("无效事件游标")
                pending = [event for event in events if event.seq > cursor]
                if not pending:
                    if events[-1].type in {"run.completed", "run.finished"}:
                        return
                    if run_id in state.finished:
                        raise RuntimeError("运行终态未持久化")
                    state.condition.wait()
                    continue
            for event in pending:
                cursor = event.seq
                yield event
                if event.type in {"run.completed", "run.finished"}:
                    return

    def _finish(self, run_id: str, status: str, answer: str = "", error: str | None = None) -> None:
        state = self.state
        events = self.session.trace(run_id)
        if events[-1].type not in {"run.completed", "run.finished"}:
            if state.stop and status != "failed":
                status = "stopped"
            if status == "completed":
                self.session.append_event(run_id, "run.completed", {"answer": answer})
            else:
                data = {"status": status}
                if error is not None:
                    data["error"] = error
                self.session.append_event(run_id, "run.finished", data)
        state.condition.notify_all()

    def _unsafe_exception(self, error: BaseException | None) -> bool:
        seen: set[int] = set()
        pending = [error] if error is not None else []
        while pending:
            current = pending.pop()
            if id(current) in seen:
                continue
            seen.add(id(current))
            if self.session.guard.contains((str(current), getattr(current, "__notes__", ()))):
                return True
            if isinstance(current, BaseExceptionGroup):
                pending.extend(current.exceptions)
            if current.__cause__ is not None:
                pending.append(current.__cause__)
            elif not current.__suppress_context__ and current.__context__ is not None:
                pending.append(current.__context__)
        return False

    def _validate_call(self, call: ToolCallContent) -> None:
        self.session.guard.check({"name": call.name, "arguments": call.arguments})
        self.session.guard.check(TypeAdapter(JsonValue).validate_json(call.arguments))
        parameters = self.registry.get(call.name).parameters.model_validate_json(call.arguments)
        self.session.guard.check(parameters)

    def _run(self, run_id: str) -> None:
        state = self.state
        turn = 0
        try:
            while True:
                with state.condition:
                    if state.shutdown:
                        return
                    if state.stop:
                        self._finish(run_id, "stopped")
                        return
                    if turn == 64:
                        self._finish(run_id, "limited")
                        return
                    messages = to_openai_messages(self.session.context(run_id))
                    tools = self.registry.declarations()
                    turn += 1
                    self.session.append_event(run_id, "model.request", {
                        "turn_index": turn, "model": self.model, "messages": messages, "tools": tools,
                    })
                    state.condition.notify_all()
                stream = _ModelStream(self.client, self.credentials, state, self.model, messages, tools)
                with state.condition:
                    state.model_stream = stream
                    if state.shutdown:
                        stream.interrupt()
                try:
                    if not stream.ready.result():
                        with state.condition:
                            if not state.shutdown:
                                self._finish(run_id, "stopped")
                        return
                    response = None
                    for event in self.session.record_model_stream(run_id, turn, stream):
                        if event.type == "model.response":
                            response = event.data
                        with state.condition:
                            state.condition.notify_all()
                finally:
                    stream.close()
                    with state.condition:
                        if state.model_stream is stream:
                            state.model_stream = None
                if response is None or response["message"] is None:
                    raise ValueError("模型响应不完整")
                message = AssistantMessage.model_validate(response["message"])
                calls = [block for block in message.content if isinstance(block, ToolCallContent)]
                with state.condition:
                    if state.shutdown:
                        return
                    if state.stop:
                        for call in calls:
                            self._validate_call(call)
                        self._finish(run_id, "stopped")
                        return
                    self.session.append_message(message, run_id)
                for call in calls:
                    with state.condition:
                        if state.shutdown:
                            return
                        if state.stop:
                            self._finish(run_id, "stopped")
                            return
                        self.session.guard.check({"name": call.name, "arguments": call.arguments})
                        self.session.append_event(run_id, "tool.started", {
                            "turn_index": turn, "tool_call_id": call.id, "name": call.name, "arguments": call.arguments,
                        })
                        state.condition.notify_all()
                        self._validate_call(call)
                        if call.name == "todo":
                            command = TodoCommand.model_validate_json(call.arguments)
                            if command.action == "delete":
                                item = next((item for item in self.session.todo_snapshot() if item["id"] == command.id), None)
                                if item is None:
                                    raise ValueError("待办任务不存在")
                                state.confirmation = (run_id, turn, call.id)
                                state.decision = None
                                self.session.append_event(run_id, "tool.confirmation_requested", {
                                    "turn_index": turn, "tool_call_id": call.id, "name": "todo", "action": "delete",
                                    "task": {"id": item["id"], "title": item["title"]},
                                })
                                state.condition.notify_all()
                                state.condition.wait_for(lambda: state.stop or state.shutdown or state.confirmation_failed or state.decision is not None)
                                decision = state.decision
                                state.confirmation = None
                                state.decision = None
                                if state.confirmation_failed:
                                    raise RuntimeError("确认事件写入失败")
                                if state.shutdown:
                                    return
                                if state.stop or decision == "rejected":
                                    self._finish(run_id, "stopped")
                                    return
                    def start_tool() -> bool:
                        with state.condition:
                            return not state.stop and not state.shutdown
                    result = self.session.execute_tool(self.registry, run_id, turn, call.id, call.name, call.arguments, start=start_tool)
                    with state.condition:
                        state.condition.notify_all()
                        if result is not None:
                            self.session.append_message(ToolResultMessage(tool_call_id=call.id, content=result), run_id)
                        if state.shutdown:
                            return
                        if state.stop:
                            self._finish(run_id, "stopped")
                            return
                        if result is None:
                            raise RuntimeError("工具未启动")
                with state.condition:
                    if state.shutdown:
                        return
                    if state.stop:
                        self._finish(run_id, "stopped")
                        return
                    applied = False
                    while state.queue:
                        queue_id, content = state.queue.popleft()
                        record = self.session.append_message(UserMessage(content=content), run_id, queue_id=queue_id)
                        self.session.append_event(run_id, "steer.applied", {"queue_id": queue_id, "message_id": record.id})
                        applied = True
                        state.condition.notify_all()
                    if not calls and not applied:
                        answer = "".join(block.text for block in message.content if isinstance(block, TextContent))
                        self._finish(run_id, "completed", answer)
                        return
        finally:
            error = sys.exception()
            try:
                with state.condition:
                    try:
                        if error is not None and not (state.shutdown and isinstance(error, asyncio.CancelledError)):
                            error_type = type(error).__name__
                            if self.session.guard.contains(error_type):
                                error_type = None
                            logging.getLogger(__name__).error("run %s failed", run_id)
                            self._finish(run_id, "failed", error=error_type)
                    finally:
                        if state.active == run_id:
                            state.active = None
                        state.finished.add(run_id)
                        if state.executor is not None:
                            state.executor.shutdown(wait=False)
                            state.executor = None
                        with self.session.live_lock, _LIVE_RUNS_LOCK:
                            _LIVE_RUNS.discard((str(self.session.path), run_id))
                        self.session.run_lock.release()
                        state.condition.notify_all()
            finally:
                current = sys.exception()
                if self._unsafe_exception(error) or self._unsafe_exception(current):
                    raise ValueError("内容包含已配置密钥") from None
