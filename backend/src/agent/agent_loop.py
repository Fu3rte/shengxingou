from __future__ import annotations

import asyncio
import logging
import sys
from typing import TYPE_CHECKING

from pydantic import JsonValue, TypeAdapter

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
from ..ai.models.openai import to_openai_messages
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


async def run(agent: Agent, run_id: str) -> None:
    state = agent.state
    turn = 0
    try:
        while True:
            async with state.condition:
                if state.shutdown:
                    return
                if state.stop:
                    await _finish(agent, run_id, "stopped")
                    return
                if turn == 64:
                    await _finish(agent, run_id, "limited")
                    return
                messages = to_openai_messages(
                    await run_sync(agent.session.context, run_id)
                )
                tools = declarations(agent.registry)
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
