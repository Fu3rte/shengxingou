import tempfile
import unittest
from pathlib import Path
from threading import Event

from openai import OpenAI

from src import server
from src.agent.agent_loop import agent_loop


class ServerTests(unittest.TestCase):
    def test_json_persistence_and_recovery(self):
        directory = Path(__file__).resolve().parents[2] / ".tool-tmp"
        directory.mkdir(exist_ok=True)
        previous = server.DATA
        with tempfile.TemporaryDirectory(dir=directory) as folder:
            server.DATA = Path(folder) / "conversations.json"
            turns = [{"id": "1", "status": "running", "messages": [{"role": "user", "content": "测试"}]}]
            server.save_turns(turns)
            self.assertEqual(server.read_turns(), turns)
            server.recover_interrupted()
            self.assertEqual(server.read_turns()[0]["status"], "interrupted")
        server.DATA = previous

    def test_limit_and_stop_before_request(self):
        with OpenAI(api_key="local-validation") as client:
            messages = [{"role": "user", "content": "测试"}]
            self.assertIsNone(agent_loop(client, "unused", messages, max_turns=0))
            stop = Event()
            stop.set()
            self.assertIsNone(agent_loop(client, "unused", messages, max_turns=64, stop=stop))
            self.assertEqual(len(messages), 1)


if __name__ == "__main__":
    unittest.main()
