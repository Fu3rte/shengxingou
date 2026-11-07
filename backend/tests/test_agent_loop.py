import tempfile
import unittest
from pathlib import Path

from openai import APIConnectionError, AsyncOpenAI

from src.agent.agent_loop import AgentLoop, close_model_client
from src.agent.session import SessionManager


class AgentLoopTests(unittest.TestCase):
    def test_session_loop_rejects_empty_message_and_exposes_tools(self):
        directory = Path(__file__).resolve().parents[2] / ".tool-tmp"
        directory.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=directory) as folder:
            session = SessionManager(Path(folder), secrets=()).create()
            client = AsyncOpenAI(api_key="test-key", max_retries=0)
            loop = AgentLoop(session, client, "test-model")
            try:
                with self.assertRaises(ValueError):
                    loop.append_message(" ")
                self.assertEqual(
                    [tool["function"]["name"] for tool in loop.registry.declarations()],
                    ["calculate", "weather", "search", "todo"],
                )
            finally:
                loop.close()
                close_model_client(client)

    def test_model_connection_error_is_reported_without_key(self):
        directory = Path(__file__).resolve().parents[2] / ".tool-tmp"
        directory.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=directory) as folder:
            session = SessionManager(Path(folder), secrets=("test-key",)).create()
            client = AsyncOpenAI(api_key="test-key", base_url="http://127.0.0.1:1/v1", max_retries=0, timeout=2)
            loop = AgentLoop(session, client, "test-model")
            try:
                run = loop.append_message("hello")
                with self.assertRaises(APIConnectionError) as raised:
                    loop.wait(run, timeout=10)
                event = loop.trace(run)[-1]
                self.assertEqual(event.type, "run.finished")
                self.assertEqual(event.data["status"], "failed")
                self.assertEqual(event.data["error"], type(raised.exception).__name__)
                self.assertNotIn("test-key", event.model_dump_json())
            finally:
                loop.close()
                close_model_client(client)


if __name__ == "__main__":
    unittest.main()
