import os
import sys
from pathlib import Path
from threading import Event
from collections.abc import Callable

from dotenv import load_dotenv
from openai import OpenAI

from .tools.calculator import TOOLS, calculate


def agent_loop(
    client: OpenAI,
    model: str,
    messages: list,
    *,
    max_turns: int | None = None,
    stop: Event | None = None,
    on_update: Callable[[], None] | None = None,
) -> str | None:
    turns = 0
    while max_turns is None or turns < max_turns:
        if stop is not None and stop.is_set():
            return None
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            tools=TOOLS,
        )
        turns += 1
        if stop is not None and stop.is_set():
            return None
        choice = response.choices[0]
        message = choice.message
        if choice.finish_reason == "stop" and not message.tool_calls:
            if message.content is None:
                raise ValueError("模型响应没有内容")
            messages.append({"role": "assistant", "content": message.content})
            return message.content
        if choice.finish_reason != "tool_calls" or not message.tool_calls:
            raise ValueError(f"模型响应未完成: {choice.finish_reason}")

        messages.append({
            "role": "assistant",
            "content": message.content,
            "tool_calls": [call.model_dump(exclude_none=True) for call in message.tool_calls],
        })
        for call in message.tool_calls:
            if stop is not None and stop.is_set():
                return None
            if call.type != "function" or call.function.name != "calculate":
                raise ValueError("未知工具")
            result = calculate(call.function.arguments)
            print(f"calculate {call.function.arguments} -> {result}", flush=True)
            messages.append({"role": "tool", "tool_call_id": call.id, "content": result})
            if on_update is not None:
                on_update()
    return None


def main() -> None:
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    model = os.environ["OPENAI_MODEL"]
    messages = [{"role": "system", "content": "你是助手。需要计算时使用计算器工具。"}]
    with OpenAI(max_retries=0, timeout=60.0) as client:
        for line in sys.stdin:
            if not line.strip():
                continue
            messages.append({"role": "user", "content": line.strip()})
            print(agent_loop(client, model, messages), flush=True)


if __name__ == "__main__":
    main()
