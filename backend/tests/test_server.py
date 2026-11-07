import tempfile
import unittest
from pathlib import Path

from src.agent.session import SessionManager
from src.agent.types import SystemMessage


class ServerTests(unittest.TestCase):
    def test_json_persistence_and_recovery(self):
        directory = Path(__file__).resolve().parents[2] / ".tool-tmp"
        directory.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=directory) as folder:
            manager = SessionManager(Path(folder), secrets=())
            session = manager.create()
            session.append_message(SystemMessage(content="测试"), None)
            session.append_event("run1", "run.started", {"input": "测试"})
            self.assertEqual(manager.open(session.id).read()[1].message.content, "测试")
            manager.recover()
            self.assertEqual(session.trace("run1")[-1].data["status"], "interrupted")
            self.assertEqual(manager.recover(), {session.id: []})


if __name__ == "__main__":
    unittest.main()
