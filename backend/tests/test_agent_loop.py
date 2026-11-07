import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from openai import APIConnectionError, AsyncOpenAI

from src.agent.runtime import append_message, close, create_agent, events, trace, wait
from src.agent.session import SessionManager
from src.agent.tools.registry import declarations, get
from src.ai.models.openai import validate_client
from src.ai.models.openai_stream import model_response
from tests.test_normalized_stream import chunk


class AgentLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_session_loop_rejects_empty_message_and_exposes_tools(self):
        directory = Path(__file__).resolve().parents[2] / ".tool-tmp"
        directory.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=directory) as folder:
            session = SessionManager(Path(folder), secrets=()).create()
            client = AsyncOpenAI(api_key="test-key", max_retries=0)
            agent = await create_agent(session, client, "test-model")
            try:
                self.assertNotIn("test-key", repr(agent))
                with self.assertRaises(ValueError):
                    await append_message(agent, " ")
                self.assertEqual(
                    [tool["function"]["name"] for tool in declarations(agent.registry)],
                    ["calculate", "weather", "search", "todo"],
                )
                self.assertEqual(get(agent.registry, "todo").execute, session.execute_todo)
                await asyncio.to_thread(session.append_event, "run", "run.started", {"input": "hello"})
                subscription = events(agent, "run", after=1)
                pending = asyncio.create_task(anext(subscription))
                await asyncio.sleep(0)
                await asyncio.to_thread(session.append_event, "run", "run.finished", {"status": "stopped"})
                self.assertEqual((await asyncio.wait_for(pending, 3)).type, "run.finished")
                await subscription.aclose()
            finally:
                await close(agent)
                await client.close()

    async def test_client_configuration_is_rechecked_before_submission_and_request(self):
        directory = Path(__file__).resolve().parents[2] / ".tool-tmp"
        directory.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=directory) as folder:
            session = SessionManager(Path(folder), secrets=()).create()
            client = AsyncOpenAI(api_key="test-key", max_retries=0)
            agent = await create_agent(session, client, "test-model")
            try:
                client._client.headers["x-api-key"] = "other-key"
                with self.assertRaisesRegex(ValueError, "固定 API key"):
                    await append_message(agent, "hello")
                del client._client.headers["x-api-key"]
                agent.client._client.event_hooks["request"].append(lambda request: None)
                with self.assertRaisesRegex(ValueError, "固定 API key"):
                    await model_response(agent, "run", 1, [], [])
                self.assertEqual(session.read()[0].type, "session")
            finally:
                await close(agent)
                await client.close()

    async def test_stream_checks_credentials_per_chunk_without_reauditing_transport(self):
        directory = Path(__file__).resolve().parents[2] / ".tool-tmp"
        directory.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=directory) as folder:
            session = SessionManager(Path(folder), secrets=()).create()
            client = AsyncOpenAI(api_key="test-key", max_retries=0)
            agent = await create_agent(session, client, "test-model")
            session.append_event("run", "run.started", {"input": "hello"})
            session.append_event("run", "model.request", {"turn_index": 1, "model": "test-model", "messages": [], "tools": []})

            class Stream:
                closed = False

                async def __aiter__(self):
                    yield chunk({"role": "assistant", "content": "safe "})
                    agent.client.api_key = "changed-key"
                    yield chunk({"content": "changed-key"})

                async def close(self):
                    self.closed = True

            stream = Stream()
            try:
                with patch.object(agent.client.chat.completions, "create", new=AsyncMock(return_value=stream)), \
                     patch("src.ai.models.openai_stream.validate_client", wraps=validate_client) as audit:
                    with self.assertRaisesRegex(ValueError, "更改密钥"):
                        await model_response(agent, "run", 1, [], [])
                    self.assertEqual(audit.call_count, 1)
                self.assertTrue(stream.closed)
                self.assertNotIn("changed-key", session.path.read_text(encoding="utf-8"))
            finally:
                await close(agent)
                await client.close()

    async def test_model_connection_error_is_reported_without_key(self):
        directory = Path(__file__).resolve().parents[2] / ".tool-tmp"
        directory.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=directory) as folder:
            manager = SessionManager(Path(folder), secrets=("test-key",))
            sessions = [manager.create(), manager.create()]
            client = AsyncOpenAI(api_key="test-key", base_url="http://127.0.0.1:1/v1", max_retries=0, timeout=2)
            agents = [await create_agent(session, client, "test-model") for session in sessions]
            try:
                first, steering, second = await asyncio.gather(
                    append_message(agents[0], "hello"), append_message(agents[0], "next"),
                    append_message(agents[1], "other"),
                )
                self.assertEqual(first, steering)
                self.assertNotEqual(first, second)
                for agent, run in zip(agents, (first, second)):
                    with self.assertRaises(APIConnectionError) as raised:
                        await wait(agent, run, timeout=10)
                    snapshot = await trace(agent, run)
                    event = snapshot[-1]
                    self.assertEqual(event.type, "run.finished")
                    self.assertEqual(event.data["status"], "failed")
                    self.assertEqual(event.data["error"], type(raised.exception).__name__)
                    self.assertNotIn("test-key", event.model_dump_json())
                self.assertEqual([event.data["content"] for event in await trace(agents[0], first)
                                  if event.type == "steer.queued"], ["next"])
            finally:
                for agent in agents:
                    await close(agent)
                await client.close()


if __name__ == "__main__":
    unittest.main()
