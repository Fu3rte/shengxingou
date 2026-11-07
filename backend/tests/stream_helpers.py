import asyncio


async def async_chunks(chunks):
    for chunk in chunks:
        yield chunk


def sync_stream(stream):
    runner = asyncio.Runner()
    try:
        while True:
            try:
                yield runner.run(anext(stream))
            except StopAsyncIteration:
                return
    finally:
        runner.run(stream.aclose())
        runner.close()
