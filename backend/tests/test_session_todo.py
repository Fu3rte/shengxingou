import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from threading import Barrier

from src.agent.session import create_session, open_session
from src.agent.tools.registry import create_registry, execute, get


class SessionTodoTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[2] / ".tool-tmp"
        root.mkdir(exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.create = partial(create_session, self.root, secrets=("secret-key",))
        self.open = partial(open_session, self.root, secrets=("secret-key",))
        self.session = self.create()
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
        self.session = self.open(self.session.id)
        self.assertEqual(self.session.todo_snapshot(), [{"id": 1, "title": "一", "status": "pending"}])
        self.session.append_event("r2", "run.started", {"input": "继续"})
        self.assertEqual(self.call("r2", 1, "c4", {"action": "add", "title": "三"})["item"]["id"], 3)
        other = self.create()
        self.assertEqual(other.todo_snapshot(), [
            {"id": 1, "title": "一", "status": "pending"},
            {"id": 3, "title": "三", "status": "pending"},
        ])
        lines = (Path(self.directory.name) / ".todo" / "todo.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual([json.loads(line) for line in lines], [{"next_id": 4}, *other.todo_snapshot()])

    def test_cross_session_updates_and_concurrent_adds(self):
        sessions = [self.create() for _ in range(8)]
        barrier = Barrier(len(sessions))

        def add(index):
            session = sessions[index]
            session.append_event("run", "run.started", {"input": "任务"})
            arguments = json.dumps({"action": "add", "title": f"任务{index}"}, ensure_ascii=False)
            session.append_event("run", "tool.started", {"turn_index": 1, "tool_call_id": "c1", "name": "todo", "arguments": arguments})
            barrier.wait()
            return json.loads(session.execute_todo("run", 1, "c1", arguments))["item"]["id"]

        with ThreadPoolExecutor(max_workers=len(sessions)) as pool:
            ids = list(pool.map(add, range(len(sessions))))
        self.assertEqual(sorted(ids), list(range(1, 9)))
        self.assertEqual(len(self.session.todo_snapshot()), 8)
        changed = self.call("r1", 1, "status", {"action": "set_status", "id": 1, "status": "completed"})["item"]
        self.assertEqual(self.open(sessions[0].id).todo_snapshot()[0], changed)
        arguments = json.dumps({"action": "delete", "id": 1})
        self.session.append_event("r1", "tool.started", {"turn_index": 2, "tool_call_id": "delete", "name": "todo", "arguments": arguments})
        self.session.append_event("r1", "tool.confirmation_requested", {"turn_index": 2, "tool_call_id": "delete", "name": "todo", "action": "delete", "task": {"id": 1, "title": changed["title"]}})
        self.session.append_event("r1", "tool.confirmation_resolved", {"turn_index": 2, "tool_call_id": "delete", "decision": "approved"})
        self.session.execute_todo("r1", 2, "delete", arguments)
        self.assertEqual(len(self.open(sessions[0].id).todo_snapshot()), 7)

    def test_registry_executes_shared_todo(self):
        other = self.create()
        other.append_event("other", "run.started", {"input": "查看"})
        first_registry = create_registry(None, self.session.todo_snapshot, todo_execute=self.session.execute_todo)
        other_registry = create_registry(None, other.todo_snapshot, todo_execute=other.execute_todo)
        arguments = '{"action":"add","title":"共享"}'
        self.session.append_event("r1", "tool.started", {"turn_index": 1, "tool_call_id": "add",
                                                        "name": "todo", "arguments": arguments})
        self.assertEqual(get(first_registry, "todo").execute, self.session.execute_todo)
        with self.assertRaises(ValueError):
            execute(first_registry, "todo", arguments)
        result = self.session.execute_tool(first_registry, "r1", 1, "add", "todo", arguments)
        self.assertEqual(json.loads(result)["item"], {"id": 1, "title": "共享", "status": "pending"})
        arguments = '{"action":"list"}'
        other.append_event("other", "tool.started", {"turn_index": 1, "tool_call_id": "list",
                                                     "name": "todo", "arguments": arguments})
        with self.assertRaisesRegex(ValueError, "会话不匹配"):
            other.execute_tool(first_registry, "other", 1, "list", "todo", arguments)
        result = other.execute_tool(other_registry, "other", 1, "list", "todo", arguments)
        self.assertEqual(json.loads(result)["items"], self.session.todo_snapshot())
        self.assertEqual(other.trace("other")[-1].type, "tool.completed")
        arguments = '{"action":"delete","id":1}'
        self.session.append_event("r1", "tool.started", {"turn_index": 2, "tool_call_id": "delete",
                                                        "name": "todo", "arguments": arguments})
        with self.assertRaisesRegex(ValueError, "尚未确认"):
            self.session.execute_tool(first_registry, "r1", 2, "delete", "todo", arguments)
        self.session.append_event("r1", "tool.confirmation_requested", {
            "turn_index": 2, "tool_call_id": "delete", "name": "todo", "action": "delete",
            "task": {"id": 1, "title": "共享"},
        })
        self.session.append_event("r1", "tool.confirmation_resolved", {
            "turn_index": 2, "tool_call_id": "delete", "decision": "approved",
        })
        result = self.session.execute_tool(first_registry, "r1", 2, "delete", "todo", arguments)
        self.assertEqual(json.loads(result)["deleted"]["id"], 1)
        self.assertEqual(other.todo_snapshot(), [])

    def test_delete_confirmation_rechecks_shared_state(self):
        self.call("r1", 1, "add", {"action": "add", "title": "共享任务"})
        arguments = json.dumps({"action": "delete", "id": 1})
        request = {"turn_index": 2, "tool_call_id": "delete", "name": "todo", "action": "delete",
                   "task": {"id": 1, "title": "共享任务"}}
        self.session.append_event("r1", "tool.started", {"turn_index": 2, "tool_call_id": "delete",
                                                         "name": "todo", "arguments": arguments})
        self.session.append_event("r1", "tool.confirmation_requested", request)
        self.session.append_event("r1", "tool.confirmation_resolved", {
            "turn_index": 2, "tool_call_id": "delete", "decision": "approved",
        })
        other = self.create()
        other.append_event("other", "run.started", {"input": "删除"})
        other.append_event("other", "tool.started", {"turn_index": 2, "tool_call_id": "delete",
                                                       "name": "todo", "arguments": arguments})
        other.append_event("other", "tool.confirmation_requested", request)
        other.append_event("other", "tool.confirmation_resolved", {
            "turn_index": 2, "tool_call_id": "delete", "decision": "approved",
        })
        other.execute_todo("other", 2, "delete", arguments)
        with self.assertRaisesRegex(ValueError, "待办删除尚未确认"):
            self.session.execute_todo("r1", 2, "delete", arguments)
        self.assertEqual(self.session.todo_snapshot(), [])
        self.assertNotIn("tool.completed", [event.type for event in self.session.trace("r1")
                                             if event.data.get("tool_call_id") == "delete"])

    def test_history_revalidates_todo_results_on_open(self):
        self.call("r1", 1, "add", {"action": "add", "title": "任务"})
        self.call("r1", 2, "list", {"action": "list"})
        self.call("r1", 3, "status", {"action": "set_status", "id": 1, "status": "completed"})
        arguments = '{"action":"delete","id":1}'
        self.session.append_event("r1", "tool.started", {"turn_index": 4, "tool_call_id": "delete", "name": "todo", "arguments": arguments})
        self.session.append_event("r1", "tool.confirmation_requested", {"turn_index": 4, "tool_call_id": "delete", "name": "todo", "action": "delete", "task": {"id": 1, "title": "任务"}})
        self.session.append_event("r1", "tool.confirmation_resolved", {"turn_index": 4, "tool_call_id": "delete", "decision": "approved"})
        self.session.execute_todo("r1", 4, "delete", arguments)
        original = self.session.path.read_text(encoding="utf-8")
        self.open(self.session.id)
        invalid = [
            ("add", '{"item":{"id":1,"title":"其他","status":"pending"}}'),
            ("add", "{"),
            ("list", '{"items":{}}'),
            ("list", '{"items":[{"id":0,"title":"任务","status":"pending"}]}'),
            ("status", '{"item":{"id":1,"title":"任务","status":"pending"}}'),
            ("delete", '{"deleted":{"id":2,"title":"任务","status":"completed"}}'),
        ]
        for call_id, result in invalid:
            with self.subTest(call_id=call_id, result=result):
                records = [json.loads(line) for line in original.splitlines()]
                event = next(record["event"] for record in records if record["type"] == "event"
                             and record["event"]["type"] == "tool.completed"
                             and record["event"]["data"]["tool_call_id"] == call_id)
                event["data"]["result"] = result
                self.session.path.write_text("".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records), encoding="utf-8")
                with self.assertRaises(ValueError):
                    self.open(self.session.id)
        self.session.path.write_text(original, encoding="utf-8")
        self.open(self.session.id)

    def test_shared_file_validation_rejects_corruption(self):
        self.call("r1", 1, "add", {"action": "add", "title": "任务"})
        path = self.session.todo_path
        original = path.read_bytes()
        for content in (b'', b'{"next_id":2}', b'{"next_id":true}\n',
                        b'{"next_id":1}\n{"id":1,"title":"x","status":"pending"}\n',
                        b'{"next_id":3}\n{"id":1,"title":"x","status":"pending"}\n'
                        b'{"id":1,"title":"y","status":"pending"}\n'):
            with self.subTest(content=content):
                path.write_bytes(content)
                with self.assertRaises(ValueError):
                    self.create().todo_snapshot()
        path.write_bytes(original)
        self.assertEqual(self.create().todo_snapshot(), [{"id": 1, "title": "任务", "status": "pending"}])

    def test_delete_confirmation_uses_current_status_after_other_session_update(self):
        self.call("r1", 1, "add", {"action": "add", "title": "任务"})
        arguments = '{"action":"delete","id":1}'
        self.session.append_event("r1", "tool.started", {"turn_index": 2, "tool_call_id": "delete", "name": "todo", "arguments": arguments})
        self.session.append_event("r1", "tool.confirmation_requested", {"turn_index": 2, "tool_call_id": "delete", "name": "todo", "action": "delete", "task": {"id": 1, "title": "任务"}})
        self.session.append_event("r1", "tool.confirmation_resolved", {"turn_index": 2, "tool_call_id": "delete", "decision": "approved"})
        other = self.create()
        other.append_event("other", "run.started", {"input": "修改"})
        update = '{"action":"set_status","id":1,"status":"completed"}'
        other.append_event("other", "tool.started", {"turn_index": 1, "tool_call_id": "status", "name": "todo", "arguments": update})
        other.execute_todo("other", 1, "status", update)
        result = json.loads(self.session.execute_todo("r1", 2, "delete", arguments))
        self.assertEqual(result["deleted"], {"id": 1, "title": "任务", "status": "completed"})
        self.assertEqual(other.todo_snapshot(), [])
        self.assertEqual([event.type for event in self.session.trace("r1")][-3:],
                         ["tool.confirmation_requested", "tool.confirmation_resolved", "tool.completed"])

    def test_delete_confirmation_rejects_mismatched_title(self):
        self.call("r1", 1, "add", {"action": "add", "title": "现有标题"})
        arguments = '{"action":"delete","id":1}'
        self.session.append_event("r1", "tool.started", {"turn_index": 2, "tool_call_id": "delete", "name": "todo", "arguments": arguments})
        self.session.append_event("r1", "tool.confirmation_requested", {"turn_index": 2, "tool_call_id": "delete", "name": "todo", "action": "delete", "task": {"id": 1, "title": "旧标题"}})
        self.session.append_event("r1", "tool.confirmation_resolved", {"turn_index": 2, "tool_call_id": "delete", "decision": "approved"})
        with self.assertRaisesRegex(ValueError, "待办删除尚未确认"):
            self.session.execute_todo("r1", 2, "delete", arguments)
        self.assertEqual(self.create().todo_snapshot(), [{"id": 1, "title": "现有标题", "status": "pending"}])
        self.assertNotIn("tool.completed", [event.type for event in self.session.trace("r1") if event.data.get("tool_call_id") == "delete"])

    def test_finished_run_does_not_modify_shared_file(self):
        arguments = '{"action":"add","title":"任务"}'
        self.session.append_event("r1", "tool.started", {"turn_index": 1, "tool_call_id": "c1", "name": "todo", "arguments": arguments})

        def finish():
            self.session.append_event("r1", "run.finished", {"status": "stopped"})
            return True

        with self.assertRaisesRegex(ValueError, "运行已经结束"):
            self.session.execute_todo("r1", 1, "c1", arguments, start=finish)
        self.assertEqual(self.session.todo_snapshot(), [])

    def test_escaped_secret_does_not_modify_todo(self):
        session = create_session(self.root, secrets=("secret",))
        session.append_event("run", "run.started", {"input": "x"})
        arguments = '{"action":"add","title":"\\u0073ecret"}'
        with self.assertRaisesRegex(ValueError, "密钥"):
            session.append_event("run", "tool.started", {"turn_index": 1, "tool_call_id": "call", "name": "todo", "arguments": arguments})
        with self.assertRaisesRegex(ValueError, "密钥"):
            session.execute_todo("run", 1, "call", arguments)
        self.assertEqual(session.todo_snapshot(), [])
        self.assertEqual([event.type for event in session.trace("run")], ["run.started"])

    def test_result_security_failure_does_not_commit_in_memory(self):
        session = create_session(self.root, secrets=("pending",))
        session.append_event("run", "run.started", {"input": "x"})
        arguments = '{"action":"add","title":"safe"}'
        session.append_event("run", "tool.started", {"turn_index": 1, "tool_call_id": "c1", "name": "todo", "arguments": arguments})
        with self.assertRaisesRegex(ValueError, "密钥"):
            session.execute_todo("run", 1, "c1", arguments)
        self.assertEqual(session.todo_snapshot(), [])
        self.assertEqual([event.type for event in session.trace("run")], ["run.started", "tool.started"])
        reopened = open_session(self.root, session.id, secrets=())
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
        self.assertEqual(len(self.open(self.session.id).todo_snapshot()), 1)


if __name__ == "__main__":
    unittest.main()
