import tempfile
import unittest
from pathlib import Path

from src.agent.session import create_session, list_sessions, open_session, recover_sessions
from src.agent.types import SystemMessage


class ServerTests(unittest.TestCase):
    def test_session_functions_initialize_missing_directories(self):
        directory = Path(__file__).resolve().parents[2] / ".tool-tmp"
        directory.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=directory) as folder:
            root = Path(folder)
            self.assertEqual(list_sessions(root / "list", secrets=()), [])
            self.assertEqual(recover_sessions(root / "recover", secrets=()), {})
            with self.assertRaises(FileNotFoundError):
                open_session(root / "open", "missing", secrets=())
            session = create_session(root / "create", secrets=())
            self.assertTrue(session.path.exists())
            for name in ("list", "recover", "open", "create"):
                self.assertTrue((root / name / ".locks").is_dir())

    def test_json_persistence_and_recovery(self):
        directory = Path(__file__).resolve().parents[2] / ".tool-tmp"
        directory.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=directory) as folder:
            root = Path(folder)
            session = create_session(root, secrets=())
            session.append_message(SystemMessage(content="测试"), None)
            session.append_event("run1", "run.started", {"input": "测试"})
            self.assertEqual(open_session(root, session.id, secrets=()).read()[1].message.content, "测试")
            recover_sessions(root, secrets=())
            self.assertEqual(session.trace("run1")[-1].data["status"], "interrupted")
            self.assertEqual(recover_sessions(root, secrets=()), {session.id: []})


if __name__ == "__main__":
    unittest.main()
