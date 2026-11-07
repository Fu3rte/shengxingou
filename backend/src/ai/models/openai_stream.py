import asyncio
from contextlib import aclosing

from ...agent.session import await_task
from ...agent.session_stream import record_model_stream_async
from .openai import client_credentials, validate_client

NOT_STARTED = object()


async def model_response(agent, run_id: str, turn: int, messages: list, tools: list):
    async with agent.state.condition:
        if agent.state.stop or agent.state.shutdown:
            return NOT_STARTED
        if validate_client(agent.client) != agent.credentials:
            raise ValueError("运行期间不能更改密钥")
        request = asyncio.create_task(agent.client.chat.completions.create(
            model=agent.model, messages=messages, tools=tools, n=1, stream=True,
            stream_options={"include_usage": True},
        ))
    stream = None
    try:
        stream = await request

        async def chunks():
            async for chunk in stream:
                if client_credentials(agent.client) != agent.credentials:
                    raise ValueError("运行期间不能更改密钥")
                yield chunk

        response = None
        async with aclosing(record_model_stream_async(agent.session, run_id, turn, chunks())) as recorded:
            async for event in recorded:
                if event.type == "model.response":
                    response = event.data
                async with agent.state.condition:
                    agent.state.condition.notify_all()
        return response
    finally:
        if not request.done():
            request.cancel()
            await asyncio.wait({request})
        if stream is None and not request.cancelled() and request.exception() is None:
            stream = request.result()
        if stream is not None:
            await await_task(asyncio.create_task(stream.close()))
