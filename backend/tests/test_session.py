import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

from src.agent.session import EventRecord, SessionManager
from src.agent.tools.registry import registry
from src.agent.types import AssistantMessage, SystemMessage, TextContent, ToolCallContent, ToolResultMessage, UserMessage
from tests.test_normalized_stream import chunk


class SessionTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[2] / ".tool-tmp"
        root.mkdir(exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(self.directory.cleanup)
        self.manager = SessionManager(Path(self.directory.name), secrets=("secret-key",))

    def test_isolation_context_recovery_and_titles(self):
        first, second = self.manager.create(), self.manager.create()
        first.set_title("标题")
        self.assertEqual(dict(self.manager.list())[first.id], "标题")
        first.append_message(SystemMessage(content="系统"), None)
        first.append_event("r1", "run.started", {"input": "问题"})
        first.append_message(UserMessage(content="问题"), "r1")
        arguments = '{"left":"1","operator":"+","right":"2"}'
        first.append_message(AssistantMessage(content=[ToolCallContent(id="c1", name="calculate", arguments=arguments)]), "r1")
        first.append_event("r1", "tool.started", {"turn_index": 1, "tool_call_id": "c1", "name": "calculate", "arguments": arguments})
        result = first.execute_tool(registry, "r1", 1, "c1", "calculate", arguments)
        first.append_message(ToolResultMessage(tool_call_id="c1", content=result), "r1")
        first.append_message(AssistantMessage(content=[TextContent(text="回答")]), "r1")
        first.append_event("r1", "run.completed", {"answer": "回答"})
        first.append_event("r2", "run.started", {"input": "失败"})
        first.append_message(UserMessage(content="失败"), "r2")
        first.append_event("r2", "run.finished", {"status": "failed"})
        first.append_event("r3", "run.started", {"input": "当前"})
        first.append_message(UserMessage(content="当前"), "r3")
        second.append_event("r1", "run.started", {"input": "其他"})
        second.append_message(UserMessage(content="其他"), "r1")
        reopened = self.manager.open(first.id)
        self.assertEqual([message.role for message in reopened.context("r3")],
                         ["system", "user", "assistant", "toolResult", "assistant", "user"])
        self.assertEqual([event.seq for event in reopened.trace("r1")], [1, 2, 3, 4])
        self.assertEqual(second.trace("r1")[0].data["input"], "其他")
        self.assertEqual(reopened.recover()[0].data["status"], "interrupted")
        self.assertEqual(reopened.recover(), [])
        self.assertEqual(reopened.trace("r1")[-1].type, "run.completed")

    def test_uncommitted_tool_result_rejected(self):
        session = self.manager.create()
        session.append_event("r1", "run.started", {"input": "x"})
        with self.assertRaisesRegex(ValueError, "未持久化"):
            session.append_message(ToolResultMessage(tool_call_id="c1", content="result"), "r1")

    def test_incomplete_tool_call_rejected(self):
        session = self.manager.create()
        session.append_event("r1", "run.started", {"input": "x"})
        session.append_message(AssistantMessage(content=[ToolCallContent(id="c1", name="f", arguments="{}")]), "r1")
        with self.assertRaises(ValueError):
            session.context("r1")

    def test_concurrent_writes(self):
        first, second = self.manager.create(), self.manager.create()
        for session in (first, second):
            session.append_event("run", "run.started", {"input": "x"})
        barrier = Barrier(20)

        def worker(index):
            barrier.wait()
            session = self.manager.open(first.id if index % 2 else second.id)
            return session.id, session.append_event("run", "steer.queued", {"index": index}).seq

        with ThreadPoolExecutor(max_workers=20) as pool:
            results = list(pool.map(worker, range(20)))
        for session in (first, second):
            self.assertEqual(sorted(seq for session_id, seq in results if session_id == session.id), list(range(2, 12)))
            self.assertEqual([event.seq for event in session.trace("run")], list(range(1, 12)))

    def test_corruption_and_ids(self):
        session = self.manager.create()
        for invalid in ("../escape", "", "a/b", "."):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.manager.open(invalid)
        session.append_event("r1", "run.started", {"input": "x"})
        original = session.path.read_bytes()
        with session.path.open("ab") as file:
            file.write(b'{"type":')
        with self.assertRaises(ValueError):
            session.recover()
        self.assertTrue(session.path.read_bytes().endswith(b'{"type":'))
        session.path.write_bytes(original)
        records = session.path.read_text(encoding="utf-8").splitlines()
        with session.path.open("a", encoding="utf-8") as file:
            file.write(records[1] + "\n")
        with self.assertRaises(ValueError):
            session.read()

    def test_model_stream_persists_only_safe_normalized_events(self):
        session = self.manager.create()
        session.append_event("r1", "run.started", {"input": "x"})
        session.append_event("r1", "model.request", {"turn_index": 1, "model": "example", "messages": [], "tools": []})
        chunks = [chunk({"role": "assistant", "content": "safe ", "vendor": "ignored"}),
                  chunk({"content": "secret-"}), chunk({"content": "key"}), chunk({}, "stop")]
        stream = session.record_model_stream("r1", 1, chunks)
        first = next(stream)
        self.assertEqual(first.data["fragments"], {"text": "safe "})
        self.assertEqual(self.manager.open(session.id).trace("r1")[-1], first)
        with self.assertRaisesRegex(ValueError, "密钥"):
            next(stream)
        self.assertNotIn("secret-key", session.path.read_text(encoding="utf-8"))
        self.assertNotIn("vendor", session.path.read_text(encoding="utf-8"))
        self.assertEqual([event.type for event in session.trace("r1")],
                         ["run.started", "model.request", "model.delta"])

    def test_completed_model_stream_records_normalized_response(self):
        session = self.manager.create()
        session.append_event("r1", "run.started", {"input": "x"})
        session.append_event("r1", "model.request", {"turn_index": 1, "model": "example", "messages": [], "tools": []})
        chunks = [chunk({"role": "assistant", "tool_calls": [{"index": 0, "id": "call1", "type": "function",
                                                              "function": {"name": "calculate", "arguments": "{}"}}]}),
                  chunk({}, "tool_calls")]
        events = list(session.record_model_stream("r1", 1, chunks))
        self.assertEqual(events[-1].type, "model.response")
        self.assertEqual(events[-1].data["message"]["content"][0]["id"], "call1")
        self.assertEqual(session.trace("r1")[-1], events[-1])

    def test_real_tool_result_recorded_after_security_check(self):
        manager = SessionManager(Path(self.directory.name), secrets=('{"result": "3"}',))
        session = manager.create()
        session.append_event("run", "run.started", {"input": "x"})
        arguments = '{"left":"1","operator":"+","right":"2"}'
        session.append_event("run", "tool.started", {"turn_index": 1, "tool_call_id": "call", "name": "calculate", "arguments": arguments})
        with self.assertRaisesRegex(ValueError, "密钥"):
            session.execute_tool(registry, "run", 1, "call", "calculate", arguments)
        self.assertEqual([event.type for event in session.trace("run")], ["run.started", "tool.started"])
        session = SessionManager(Path(self.directory.name), secrets=()).open(session.id)
        self.assertEqual(session.execute_tool(registry, "run", 1, "call", "calculate", arguments), '{"result": "3"}')
        self.assertEqual(session.trace("run")[-1].type, "tool.completed")

    def test_request_limit(self):
        session = self.manager.create()
        session.append_event("r1", "run.started", {"input": "x"})
        for turn in range(1, 65):
            session.append_event("r1", "model.request", {"turn_index": turn, "model": "example", "messages": [], "tools": []})
        with self.assertRaisesRegex(ValueError, "上限"):
            session.append_event("r1", "model.request", {"turn_index": 65, "model": "example", "messages": [], "tools": []})
        self.assertEqual(len(session.trace("r1")), 65)

    def test_invalid_version_rejected(self):
        session = self.manager.create()
        header = session.path.read_text(encoding="utf-8")
        session.path.write_text(header.replace('"version":1', '"version":true'), encoding="utf-8")
        with self.assertRaises(ValueError):
            session.read()
        session.path.write_text(header.replace('"version":1,', ''), encoding="utf-8")
        with self.assertRaises(ValueError):
            session.read()

    def test_secret_rejection_and_confirmation(self):
        session = self.manager.create()
        with self.assertRaises(ValueError):
            session.append_event("r1", "run.started", {"input": "secret-key"})
        self.assertEqual(len(session.read()), 1)
        session.append_event("r1", "run.started", {"input": "x"})
        with self.assertRaises(ValueError):
            session.append_event("r1", "model.request", {"turn_index": 1, "model": "example", "messages": [], "tools": [], "headers": {}})
        with self.assertRaises(ValueError):
            session.append_event("r1", "model.response", {"turn_index": 1})
        request = {"turn_index": 1, "tool_call_id": "c1", "name": "todo", "action": "delete", "task": {"id": 1, "title": "任务"}}
        session.append_event("r1", "tool.confirmation_requested", request)
        result = {"turn_index": 1, "tool_call_id": "c1", "decision": "approved"}
        session.append_event("r1", "tool.confirmation_resolved", result)
        with self.assertRaises(ValueError):
            session.append_event("r1", "tool.confirmation_resolved", result)
        with self.assertRaises(ValueError):
            session.append_event("r1", "model.delta", {"turn_index": 1, "text": "safe"})
        self.assertEqual(len([r for r in session.read() if isinstance(r, EventRecord)]), 3)


if __name__ == "__main__":
    unittest.main()
