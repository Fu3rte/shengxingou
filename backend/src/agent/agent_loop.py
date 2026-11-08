from __future__ import annotations

import asyncio
import logging
import json
import sys
from typing import TYPE_CHECKING

from pydantic import JsonValue, TypeAdapter

from .compact import PROMPT, Summary, size, split_recent
from .security import check, contains
from .session import release_run, run_sync
from .tools.registry import declarations, get
from .types import (
    AssistantMessage,
    TextContent,
    ToolCallContent,
    ToolResultMessage,
    UserMessage,
)
from ..ai.models.openai import (
    client_credentials,
    from_openai_response,
    to_openai_messages,
    validate_client,
)
from ..ai.models.openai_stream import NOT_STARTED, model_response as _model_response

if TYPE_CHECKING:
    from .runtime import Agent


async def _finish(
    agent: Agent, run_id: str, status: str, answer: str = "", error: str | None = None
) -> None:
    state = agent.state
    snapshot = await run_sync(agent.session.trace, run_id)
    if snapshot[-1].type not in {"run.completed", "run.finished"}:
        if state.stop and status != "failed":
            status = "stopped"
        if status == "completed":
            await run_sync(
                agent.session.append_event, run_id, "run.completed", {"answer": answer}
            )
        else:
            data = {"status": status}
            if error is not None:
                data["error"] = error
            await run_sync(agent.session.append_event, run_id, "run.finished", data)
    state.condition.notify_all()


def _unsafe_exception(agent: Agent, error: BaseException | None) -> bool:
    seen: set[int] = set()
    pending = [error] if error is not None else []
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if contains(
            agent.session.guard, (str(current), getattr(current, "__notes__", ()))
        ):
            return True
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        elif not current.__suppress_context__ and current.__context__ is not None:
            pending.append(current.__context__)
    return False


def _validate_call(agent: Agent, call: ToolCallContent) -> None:
    check(agent.session.guard, {"name": call.name, "arguments": call.arguments})
    check(agent.session.guard, TypeAdapter(JsonValue).validate_json(call.arguments))
    parameters = get(agent.registry, call.name).parameters.model_validate_json(
        call.arguments
    )
    check(agent.session.guard, parameters)


async def _compact_response(agent: Agent, messages: list):
    async with agent.state.condition:
        if agent.state.stop or agent.state.shutdown:
            return NOT_STARTED
        if validate_client(agent.client) != agent.credentials:
            raise ValueError("运行期间不能更改密钥")
        request = asyncio.create_task(
            agent.client.chat.completions.create(
                model=agent.model,
                messages=messages,
                n=1,
                stream=False,
            )
        )
    try:
        response = await request
        if client_credentials(agent.client) != agent.credentials:
            raise ValueError("运行期间不能更改密钥")
        message = from_openai_response(response)
        check(agent.session.guard, message)
        if any(not isinstance(block, TextContent) for block in message.content):
            raise ValueError("摘要不能包含工具调用")
        text = "".join(block.text for block in message.content)
        check(agent.session.guard, text)
        summary = Summary.model_validate_json(text)
        check(agent.session.guard, summary)
        return summary.model_dump(mode="json")
    finally:
        if not request.done():
            request.cancel()
            await asyncio.wait({request})


async def run(agent: Agent, run_id: str) -> None:
    state = agent.state
    turn = 0
    requests = 0
    just_compacted = False
    try:
        while True:
            async with state.condition:
                if state.shutdown:
                    return
                if state.stop:
                    await _finish(agent, run_id, "stopped")
                    return
                messages = to_openai_messages(
                    await run_sync(agent.session.context, run_id)
                )
                tools = declarations(agent.registry)
                input_size = size(messages) + size(tools)
                if just_compacted and input_size > 80000:
                    raise ValueError("上下文超过输入上限")
                if requests == 64:
                    await _finish(agent, run_id, "limited")
                    return
                older = []
                compact = None
                if input_size >= 72000 and not just_compacted:
                    _, compact, recent = await run_sync(
                        agent.session.context_parts, run_id
                    )
                    older = split_recent(recent)
                is_compact = bool(older)
                if is_compact:
                    previous = compact.event.data["summary"] if compact else None
                    history = to_openai_messages([r.message for r in older])
                    summary_messages = [
                        {"role": "system", "content": PROMPT},
                        {
                            "role": "user",
                            "content": json.dumps(
                                {"previous_summary": previous, "history": history},
                                ensure_ascii=False,
                            ),
                        },
                    ]
                    if size(summary_messages) + size([]) > 80000:
                        raise ValueError("摘要请求超过输入上限")
                    await run_sync(
                        agent.session.append_event,
                        run_id,
                        "compact.request",
                        {
                            "model": agent.model,
                            "messages": summary_messages,
                            "tools": [],
                        },
                    )
                    state.model_task = asyncio.create_task(
                        _compact_response(agent, summary_messages)
                    )
                else:
                    if input_size > 80000:
                        raise ValueError("上下文超过输入上限")
                    turn += 1
                    await run_sync(
                        agent.session.append_event,
                        run_id,
                        "model.request",
                        {
                            "turn_index": turn,
                            "model": agent.model,
                            "messages": messages,
                            "tools": tools,
                        },
                    )
                    state.model_task = asyncio.create_task(
                        _model_response(agent, run_id, turn, messages, tools)
                    )
                requests += 1
                just_compacted = False
                model_task = state.model_task
                state.condition.notify_all()
            try:
                response = await model_task
            except asyncio.CancelledError:
                if not state.shutdown:
                    raise
                return
            finally:
                async with state.condition:
                    if state.model_task is model_task:
                        state.model_task = None
            if response is NOT_STARTED:
                async with state.condition:
                    if not state.shutdown:
                        await _finish(agent, run_id, "stopped")
                return
            if is_compact:
                async with state.condition:
                    if state.shutdown:
                        return
                    if state.stop:
                        await _finish(agent, run_id, "stopped")
                        return
                    covered = list(compact.event.data["covered"]) if compact else []
                    covered.extend(r.id for r in older)
                    await run_sync(
                        agent.session.append_event,
                        run_id,
                        "compact.completed",
                        {
                            "summary": response,
                            "covered": covered,
                            "previous": compact.id if compact else None,
                        },
                    )
                    state.condition.notify_all()
                    just_compacted = True
                continue
            if response is None or response["message"] is None:
                raise ValueError("模型响应不完整")
            message = AssistantMessage.model_validate(response["message"])
            calls = [
                block for block in message.content if isinstance(block, ToolCallContent)
            ]
            async with state.condition:
                if state.shutdown:
                    return
                if state.stop:
                    for call in calls:
                        _validate_call(agent, call)
                    await _finish(agent, run_id, "stopped")
                    return
                await run_sync(agent.session.append_message, message, run_id)
            for call in calls:
                async with state.condition:
                    if state.shutdown:
                        return
                    if state.stop:
                        await _finish(agent, run_id, "stopped")
                        return
                    check(
                        agent.session.guard,
                        {"name": call.name, "arguments": call.arguments},
                    )
                    await run_sync(
                        agent.session.append_event,
                        run_id,
                        "tool.started",
                        {
                            "turn_index": turn,
                            "tool_call_id": call.id,
                            "name": call.name,
                            "arguments": call.arguments,
                        },
                    )
                    state.condition.notify_all()
                    _validate_call(agent, call)
                    tool = get(agent.registry, call.name)
                    request = (
                        await run_sync(
                            tool.confirmation,
                            tool.parameters.model_validate_json(call.arguments),
                        )
                        if tool.confirmation
                        else None
                    )
                    if request is not None:
                        state.confirmation = (run_id, turn, call.id)
                        state.decision = None
                        await run_sync(
                            agent.session.append_event,
                            run_id,
                            "tool.confirmation_requested",
                            {
                                "turn_index": turn,
                                "tool_call_id": call.id,
                                **request,
                            },
                        )
                        state.condition.notify_all()
                        await state.condition.wait_for(
                            lambda: (
                                state.stop
                                or state.shutdown
                                or state.confirmation_failed
                                or state.decision is not None
                            )
                        )
                        decision = state.decision
                        state.confirmation = None
                        state.decision = None
                        if state.confirmation_failed:
                            raise RuntimeError("确认事件写入失败")
                        if state.shutdown:
                            return
                        if state.stop or decision == "rejected":
                            await _finish(agent, run_id, "stopped")
                            return

                def start_tool() -> bool:
                    with state.control:
                        return not state.stop and not state.shutdown

                result = await run_sync(
                    agent.session.execute_tool,
                    agent.registry,
                    run_id,
                    turn,
                    call.id,
                    call.name,
                    call.arguments,
                    start=start_tool,
                )
                async with state.condition:
                    state.condition.notify_all()
                    if result is not None:
                        await run_sync(
                            agent.session.append_message,
                            ToolResultMessage(tool_call_id=call.id, content=result),
                            run_id,
                        )
                    if state.shutdown:
                        return
                    if state.stop:
                        await _finish(agent, run_id, "stopped")
                        return
                    if result is None:
                        raise RuntimeError("工具未启动")
            async with state.condition:
                if state.shutdown:
                    return
                if state.stop:
                    await _finish(agent, run_id, "stopped")
                    return
                applied = False
                while state.queue:
                    queue_id, content = state.queue.popleft()
                    record = await run_sync(
                        agent.session.append_message,
                        UserMessage(content=content),
                        run_id,
                        queue_id=queue_id,
                    )
                    await run_sync(
                        agent.session.append_event,
                        run_id,
                        "steer.applied",
                        {"queue_id": queue_id, "message_id": record.id},
                    )
                    applied = True
                    state.condition.notify_all()
                if not calls and not applied:
                    answer = "".join(
                        block.text
                        for block in message.content
                        if isinstance(block, TextContent)
                    )
                    await _finish(agent, run_id, "completed", answer)
                    return
    finally:
        error = sys.exception()
        try:
            async with state.condition:
                try:
                    if error is not None and not (
                        state.shutdown and isinstance(error, asyncio.CancelledError)
                    ):
                        error_type = type(error).__name__
                        if contains(agent.session.guard, error_type):
                            error_type = None
                        logging.getLogger(__name__).error("run %s failed", run_id)
                        await _finish(agent, run_id, "failed", error=error_type)
                finally:
                    if state.active == run_id:
                        state.active = None
                    state.finished.add(run_id)
                    await run_sync(release_run, agent.session, run_id)
                    state.condition.notify_all()
        finally:
            current = sys.exception()
            if _unsafe_exception(agent, error) or _unsafe_exception(agent, current):
                raise ValueError("内容包含已配置密钥") from None
