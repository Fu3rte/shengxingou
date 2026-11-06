import json
import tempfile
import unittest
from pathlib import Path

from src.agent.session import SessionManager


class SessionTodoTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[2] / ".tool-tmp"
        root.mkdir(exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(self.directory.cleanup)
        self.manager = SessionManager(Path(self.directory.name), secrets=("secret-key",))
        self.session = self.manager.create()
        self.session.append_event("r1", "run.started", {"input": "任务"})

    def call(self, run_id, index, call_id, command):
        arguments = json.dumps(command, ensure_ascii=False)
        self.session.append_event(run_id, "tool.started", {"turn_index": index, "tool_call_id": call_id,
                                                           "name": "todo", "arguments": arguments})
        return json.loads(self.session.execute_todo(run_id, index, call_id, arguments))

    def test_recovery_failed_run_and_deleted_max_id(self):
        self.assertEqual(self.call("r1", 1, "c1", {"action": "add", "title": "一"})["item"]["id"], 1)
        self.assertEqual(self.call("r1", 2, "c2", {"action": "add", "title": "二"})["item"]["id"], 2)
        arguments = json.dumps({"action": "delete", "id": 2})
        self.session.append_event("r1", "tool.started", {"turn_index": 3, "tool_call_id": "c3", "name": "todo", "arguments": arguments})
        request = {"turn_index": 3, "tool_call_id": "c3", "name": "todo", "action": "delete", "task": {"id": 2, "title": "二"}}
        self.session.append_event("r1", "tool.confirmation_requested", request)
        with self.assertRaises(ValueError):
            self.session.execute_todo("r1", 3, "c3", arguments)
        self.session.append_event("r1", "tool.confirmation_resolved", {"turn_index": 3, "tool_call_id": "c3", "decision": "approved"})
        self.assertEqual(json.loads(self.session.execute_todo("r1", 3, "c3", arguments))["deleted"]["id"], 2)
        self.session.append_event("r1", "run.finished", {"status": "failed"})
        self.session = self.manager.open(self.session.id)
        self.assertEqual(self.session.todo_snapshot(), [{"id": 1, "title": "一", "status": "pending"}])
        self.session.append_event("r2", "run.started", {"input": "继续"})
        self.assertEqual(self.call("r2", 1, "c4", {"action": "add", "title": "三"})["item"]["id"], 3)
        other = self.manager.create()
        self.assertEqual(other.todo_snapshot(), [])

    def test_result_security_failure_does_not_commit_in_memory(self):
        manager = SessionManager(Path(self.directory.name), secrets=("pending",))
        session = manager.create()
        session.append_event("run", "run.started", {"input": "x"})
        arguments = '{"action":"add","title":"safe"}'
        session.append_event("run", "tool.started", {"turn_index": 1, "tool_call_id": "c1", "name": "todo", "arguments": arguments})
        with self.assertRaisesRegex(ValueError, "密钥"):
            session.execute_todo("run", 1, "c1", arguments)
        self.assertEqual(session.todo_snapshot(), [])
        self.assertEqual([event.type for event in session.trace("run")], ["run.started", "tool.started"])
        reopened = SessionManager(Path(self.directory.name), secrets=()).open(session.id)
        self.assertEqual(json.loads(reopened.execute_todo("run", 1, "c1", arguments))["item"]["id"], 1)

    def test_rejection_stops_run_and_uncommitted_change_isolated(self):
        self.call("r1", 1, "c1", {"action": "add", "title": "一"})
        with self.assertRaises(ValueError):
            self.call("r1", 2, "c2", {"action": "add", "title": "secret-key"})
        self.assertEqual(len(self.session.todo_snapshot()), 1)
        arguments = json.dumps({"action": "delete", "id": 1})
        self.session.append_event("r1", "tool.started", {"turn_index": 3, "tool_call_id": "c3", "name": "todo", "arguments": arguments})
        self.session.append_event("r1", "tool.confirmation_requested", {"turn_index": 3, "tool_call_id": "c3", "name": "todo", "action": "delete", "task": {"id": 1, "title": "一"}})
        self.session.append_event("r1", "tool.confirmation_resolved", {"turn_index": 3, "tool_call_id": "c3", "decision": "rejected"})
        self.assertEqual(self.session.trace("r1")[-1].data["status"], "stopped")
        with self.assertRaises(ValueError):
            self.session.execute_todo("r1", 3, "c3", arguments)
        self.assertEqual(len(self.manager.open(self.session.id).todo_snapshot()), 1)


if __name__ == "__main__":
    unittest.main()
