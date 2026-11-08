import asyncio
import json
import stat
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from threading import Barrier, Event
from types import SimpleNamespace
from unittest.mock import patch

from src.agent.session import create_session, list_sessions, open_session, run_sync
from src.agent.session_records import EventRecord
from src.agent.session_stream import record_model_stream_async
from src.agent.tools.registry import create_registry
from src.agent.tools.todo import TodoList
from src.agent.tools.search import search
from src.agent.types import AssistantMessage, SystemMessage, TextContent, ToolCallContent, ToolResultMessage, UserMessage
from tests.test_normalized_stream import chunk
from tests.stream_helpers import async_chunks, sync_stream


registry = create_registry(TodoList())


def record_model_stream(session, run_id, turn_index, chunks):
    return sync_stream(record_model_stream_async(session, run_id, turn_index, async_chunks(chunks)))


class SessionTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[2] / ".tool-tmp"
        root.mkdir(exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.create = partial(create_session, self.root, secrets=("secret-key",))
        self.open = partial(open_session, self.root, secrets=("secret-key",))

    def test_isolation_context_recovery_and_titles(self):
        first, second = self.create(), self.create()
        first.set_title("标题")
        self.assertEqual(dict(list_sessions(self.root, secrets=("secret-key",)))[first.id], "标题")
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
        reopened = self.open(first.id)
        self.assertEqual([message.role for message in reopened.context("r3")],
                         ["system", "user", "assistant", "toolResult", "assistant", "user"])
        self.assertEqual([event.seq for event in reopened.trace("r1")], [1, 2, 3, 4])
        self.assertEqual(second.trace("r1")[0].data["input"], "其他")
        self.assertEqual(reopened.recover()[0].data["status"], "interrupted")
        self.assertEqual(reopened.recover(), [])
        self.assertEqual(reopened.trace("r1")[-1].type, "run.completed")

    def test_append_event_reuses_validated_records_and_rejects_duplicate_id(self):
        session = self.create()
        with patch.object(session, "_read", wraps=session._read) as read:
            session.append_event("run", "run.started", {"input": "x"})
            self.assertEqual(read.call_count, 1)
        existing_id = session.read()[-1].id
        before = session.path.read_bytes()
        with patch("src.agent.session.uuid4", return_value=SimpleNamespace(hex=existing_id)):
            with self.assertRaisesRegex(ValueError, "ID 重复"):
                session.append_event("run", "run.finished", {"status": "stopped"})
        self.assertEqual(session.path.read_bytes(), before)

    def test_model_writer_rejects_non_model_records(self):
        session = self.create()
        event = session.append_event("run", "run.started", {"input": "x"})
        before = session.path.read_bytes()
        record = EventRecord(id="invalid", timestamp=event.time, event=event)
        with self.assertRaisesRegex(ValueError, "模型流记录类型无效"):
            session.append_model_record(record, ids=set())
        self.assertEqual(session.path.read_bytes(), before)

    def test_uncommitted_tool_result_rejected(self):
        session = self.create()
        session.append_event("r1", "run.started", {"input": "x"})
        with self.assertRaisesRegex(ValueError, "未持久化"):
            session.append_message(ToolResultMessage(tool_call_id="c1", content="result"), "r1")

    def test_incomplete_tool_call_rejected(self):
        session = self.create()
        session.append_event("r1", "run.started", {"input": "x"})
        session.append_message(AssistantMessage(content=[ToolCallContent(id="c1", name="f", arguments="{}")]), "r1")
        with self.assertRaises(ValueError):
            session.context("r1")

    def test_concurrent_writes(self):
        first, second = self.create(), self.create()
        for session in (first, second):
            session.append_event("run", "run.started", {"input": "x"})
        barrier = Barrier(20)

        def worker(index):
            barrier.wait()
            session = self.open(first.id if index % 2 else second.id)
            return session.id, session.append_event("run", "steer.queued", {"queue_id": str(index), "content": "x"}).seq

        with ThreadPoolExecutor(max_workers=20) as pool:
            results = list(pool.map(worker, range(20)))
        for session in (first, second):
            self.assertEqual(sorted(seq for session_id, seq in results if session_id == session.id), list(range(2, 12)))
            self.assertEqual([event.seq for event in session.trace("run")], list(range(1, 12)))

    def test_cross_process_run_lock_and_recovery(self):
        session = self.create()
        session.append_event("run", "run.started", {"input": "x"})
        code = ("import sys; from pathlib import Path; from src.agent.session import open_session; "
                "open_session(Path(sys.argv[1]), sys.argv[2], secrets=()).recover()")
        with session.run_lock:
            result = subprocess.run([sys.executable, "-c", code, str(self.root), session.id],
                                    capture_output=True, text=True, timeout=15)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Timeout", result.stderr)
        self.assertEqual(session.recover()[0].data["status"], "interrupted")

    def test_cancelled_sync_operation_finishes_once(self):
        path = Path(self.directory.name) / "operation.txt"
        started, release = Event(), Event()

        def write():
            started.set()
            release.wait()
            with path.open("a", encoding="utf-8") as file:
                file.write("done")

        async def cancel_twice():
            task = asyncio.create_task(run_sync(write))
            await asyncio.to_thread(started.wait)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task

        asyncio.run(cancel_twice())
        self.assertEqual(path.read_text(encoding="utf-8"), "done")

    def test_steering_links_queued_and_appended_message(self):
        session = self.create()
        session.append_event("run", "run.started", {"input": "x"})
        earlier = session.append_message(UserMessage(content="change"), "run")
        session.append_event("run", "steer.queued", {"queue_id": "q1", "content": "change"})
        with self.assertRaises(ValueError):
            session.append_event("run", "steer.applied", {"queue_id": "q1", "message_id": earlier.id})
        unmarked = session.append_message(UserMessage(content="change"), "run")
        with self.assertRaises(ValueError):
            session.append_event("run", "steer.applied", {"queue_id": "q1", "message_id": unmarked.id})
        message = session.append_message(UserMessage(content="change"), "run", queue_id="q1")
        session.append_event("run", "steer.applied", {"queue_id": "q1", "message_id": message.id})
        with self.assertRaises(ValueError):
            session.append_event("run", "steer.queued", {"queue_id": "q1", "content": "change"})
        with self.assertRaises(ValueError):
            session.append_event("run", "steer.applied", {"queue_id": "q1", "message_id": message.id})
        self.assertEqual(self.open(session.id).trace("run")[-1].data["message_id"], message.id)

    def test_steering_fifo_rejection_and_run_isolation(self):
        session = self.create()
        session.append_event("r1", "run.started", {"input": "x"})
        for queue_id in ("q1", "q2"):
            session.append_event("r1", "steer.queued", {"queue_id": queue_id, "content": queue_id})
        second = session.append_message(UserMessage(content="q2"), "r1", queue_id="q2")
        self.assertEqual(session.context("r1"), [])
        before = session.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "顺序"):
            session.append_event("r1", "steer.applied", {"queue_id": "q2", "message_id": second.id})
        self.assertEqual(session.path.read_bytes(), before)
        session = self.open(session.id)
        first = session.append_message(UserMessage(content="q1"), "r1", queue_id="q1")
        session.append_event("r1", "steer.applied", {"queue_id": "q1", "message_id": first.id})
        self.assertEqual([message.content for message in session.context("r1")], ["q1"])
        with self.assertRaisesRegex(ValueError, "顺序"):
            session.append_event("r1", "steer.applied", {"queue_id": "q2", "message_id": second.id})
        second = session.append_message(UserMessage(content="q2"), "r1", queue_id="q2")
        session.append_event("r1", "steer.applied", {"queue_id": "q2", "message_id": second.id})
        self.assertEqual([message.content for message in session.context("r1")], ["q1", "q2"])
        session.append_event("r1", "steer.queued", {"queue_id": "pending", "content": "old"})
        self.assertEqual(session.recover()[0].data["status"], "interrupted")
        session = self.open(session.id)
        session.append_event("r2", "run.started", {"input": "new"})
        session.append_event("r2", "steer.queued", {"queue_id": "pending", "content": "new"})
        message = session.append_message(UserMessage(content="new"), "r2", queue_id="pending")
        session.append_event("r2", "steer.applied", {"queue_id": "pending", "message_id": message.id})
        self.assertEqual(self.open(session.id).trace("r2")[-1].data["message_id"], message.id)

    def test_steering_event_write_failure_and_recovery(self):
        session = self.create()
        session.append_event("r1", "run.started", {"input": "initial"})
        session.append_message(UserMessage(content="initial"), "r1")
        session.append_event("r1", "steer.queued", {"queue_id": "q1", "content": "queued"})
        message = session.append_message(UserMessage(content="queued"), "r1", queue_id="q1")
        before = session.path.read_bytes()
        original_mode = stat.S_IMODE(session.path.stat().st_mode)
        session.path.chmod(stat.S_IREAD)
        try:
            with self.assertRaises(PermissionError):
                session.append_event("r1", "steer.applied", {"queue_id": "q1", "message_id": message.id})
        finally:
            session.path.chmod(original_mode)
        self.assertEqual(session.path.read_bytes(), before)
        session = self.open(session.id)
        self.assertEqual([item.content for item in session.context("r1")], ["initial"])
        session.append_event("r1", "steer.applied", {"queue_id": "q1", "message_id": message.id})
        self.assertEqual([item.content for item in session.context("r1")], ["initial", "queued"])
        session.append_event("r1", "steer.queued", {"queue_id": "q2", "content": "pending"})
        session.append_message(UserMessage(content="pending"), "r1", queue_id="q2")
        self.assertEqual(session.recover()[0].data["status"], "interrupted")
        session = self.open(session.id)
        self.assertEqual([item.content for item in session.context("r1")], ["initial", "queued"])
        session.append_event("r2", "run.started", {"input": "next"})
        session.append_message(UserMessage(content="next"), "r2")
        self.assertEqual([item.content for item in session.context("r2")], ["next"])

    def test_steering_marker_validation_and_completed_context(self):
        session = self.create()
        session.append_event("r1", "run.started", {"input": "initial"})
        session.append_message(UserMessage(content="initial"), "r1")
        session.append_event("r1", "steer.queued", {"queue_id": "q1", "content": "same"})
        session.append_event("r1", "steer.queued", {"queue_id": "q2", "content": "same"})
        for message, run_id, queue_id in (
            (UserMessage(content="same"), "r1", "missing"),
            (UserMessage(content="wrong"), "r1", "q1"),
            (AssistantMessage(content=[]), "r1", "q1"),
            (SystemMessage(content="same"), None, "q1"),
            (UserMessage(content="same"), "r1", ""),
        ):
            with self.subTest(run_id=run_id, queue_id=queue_id, role=message.role), self.assertRaises(ValueError):
                session.append_message(message, run_id, queue_id=queue_id)
        second = session.append_message(UserMessage(content="same"), "r1", queue_id="q2")
        with self.assertRaises(ValueError):
            session.append_event("r1", "steer.applied", {"queue_id": "q1", "message_id": second.id})
        first = session.append_message(UserMessage(content="same"), "r1", queue_id="q1")
        session.append_event("r1", "steer.applied", {"queue_id": "q1", "message_id": first.id})
        session.append_event("r1", "run.completed", {"answer": "done"})
        session.append_event("r2", "run.started", {"input": "next"})
        self.assertEqual([item.content for item in self.open(session.id).context("r2")], ["initial", "same"])

    def test_steering_invalid_marker_rejected_on_rebuild(self):
        session = self.create()
        session.append_event("run", "run.started", {"input": "x"})
        session.append_event("run", "steer.queued", {"queue_id": "q1", "content": "x"})
        session.append_message(UserMessage(content="x"), "run", queue_id="q1")
        lines = session.path.read_text(encoding="utf-8").splitlines()
        message = json.loads(lines[-1])
        message["queue_id"] = "missing"
        lines[-1] = json.dumps(message)
        session.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        before = session.path.read_bytes()
        with self.assertRaises(ValueError):
            self.open(session.id)
        with self.assertRaises(ValueError):
            session.recover()
        self.assertEqual(session.path.read_bytes(), before)

    def test_steering_fifo_rejected_on_rebuild(self):
        session = self.create()
        session.append_event("run", "run.started", {"input": "x"})
        for queue_id in ("q1", "q2"):
            session.append_event("run", "steer.queued", {"queue_id": queue_id, "content": queue_id})
        message = session.append_message(UserMessage(content="q2"), "run", queue_id="q2")
        last_event = next(record for record in reversed(session.read()) if isinstance(record, EventRecord))
        forged = last_event.model_dump(mode="json")
        forged["id"] = "forged"
        forged["event"].update(seq=4, type="steer.applied", data={"queue_id": "q2", "message_id": message.id})
        with session.path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(forged) + "\n")
        before = session.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "顺序"):
            self.open(session.id)
        with self.assertRaisesRegex(ValueError, "顺序"):
            session.recover()
        self.assertEqual(session.path.read_bytes(), before)

    def test_corruption_and_ids(self):
        session = self.create()
        for invalid in ("../escape", "", "a/b", "."):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.open(invalid)
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
        session = self.create()
        session.append_event("r1", "run.started", {"input": "x"})
        session.append_event("r1", "model.request", {"turn_index": 1, "model": "example", "messages": [], "tools": []})
        chunks = [chunk({"role": "assistant", "content": "safe ", "vendor": "ignored"}),
                  chunk({"content": "secret-"}), chunk({"content": "key"}), chunk({}, "stop")]
        stream = record_model_stream(session, "r1", 1, chunks)
        first = next(stream)
        self.assertEqual(first.data["fragments"], {"text": "safe "})
        self.assertEqual(self.open(session.id).trace("r1")[-1], first)
        self.assertIsNone(next(stream).data["message"])
        with self.assertRaisesRegex(ValueError, "密钥"):
            next(stream)
        self.assertNotIn("secret-key", session.path.read_text(encoding="utf-8"))
        self.assertNotIn("vendor", session.path.read_text(encoding="utf-8"))
        self.assertEqual([event.type for event in session.trace("r1")],
                         ["run.started", "model.request", "model.delta", "model.response"])

    def test_model_stream_rechecks_concurrent_session_writes(self):
        session = self.create()
        session.append_event("r1", "run.started", {"input": "x"})
        session.append_event("r1", "model.request", {"turn_index": 1, "model": "example", "messages": [], "tools": []})
        stream = record_model_stream(session, "r1", 1, [chunk({"role": "assistant", "content": "a"}),
                                                       chunk({"content": "b"}), chunk({}, "stop")])
        first = next(stream)
        other = self.open(session.id)
        queued = other.append_event("r1", "steer.queued", {"queue_id": "q1", "content": "next"})
        remaining = list(stream)
        events = session.trace("r1")
        self.assertEqual([event.seq for event in events], list(range(1, len(events) + 1)))
        self.assertEqual([event.seq for event in (first, queued, *remaining)], [3, 4, 5, 6])
        self.assertEqual(events[-1].data["message"]["content"][0]["text"], "ab")

        async def check_async_stream():
            session = self.create()
            session.append_event("r1", "run.started", {"input": "x"})
            session.append_event("r1", "model.request", {"turn_index": 1, "model": "example", "messages": [], "tools": []})

            async def chunks():
                for item in (chunk({"role": "assistant", "content": "a"}), chunk({"content": "b"}), chunk({}, "stop")):
                    yield item

            stream = record_model_stream_async(session, "r1", 1, chunks())
            first = await anext(stream)
            other = self.open(session.id)
            queued = await asyncio.to_thread(other.append_event, "r1", "steer.queued", {"queue_id": "q1", "content": "next"})
            remaining = [event async for event in stream]
            self.assertEqual([event.seq for event in (first, queued, *remaining)], [3, 4, 5, 6])
            self.assertEqual(session.trace("r1")[-1].data["message"]["content"][0]["text"], "ab")

        asyncio.run(check_async_stream())

    def test_model_stream_stops_after_concurrent_terminal_event(self):
        session = self.create()
        session.append_event("r1", "run.started", {"input": "x"})
        session.append_event("r1", "model.request", {"turn_index": 1, "model": "example", "messages": [], "tools": []})
        stream = record_model_stream(session, "r1", 1, [chunk({"role": "assistant", "content": "a"}),
                                                       chunk({"content": "b"}), chunk({}, "stop")])
        next(stream)
        self.open(session.id).append_event("r1", "run.finished", {"status": "stopped"})
        with self.assertRaisesRegex(ValueError, "已经结束"):
            next(stream)
        self.assertEqual(session.trace("r1")[-1].type, "run.finished")

    def test_model_stream_detects_external_corruption(self):
        session = self.create()
        session.append_event("r1", "run.started", {"input": "x"})
        session.append_event("r1", "model.request", {"turn_index": 1, "model": "example", "messages": [], "tools": []})
        stream = record_model_stream(session, "r1", 1, [chunk({"role": "assistant", "content": "a"}),
                                                       chunk({"content": "b"}), chunk({}, "stop")])
        next(stream)
        with session.path.open("ab") as file:
            file.write(b"{")
        with self.assertRaisesRegex(ValueError, "残缺"):
            next(stream)

    def test_escaped_tool_argument_is_not_persisted(self):
        session = self.create()
        session.append_event("r1", "run.started", {"input": "x"})
        session.append_event("r1", "model.request", {"turn_index": 1, "model": "example", "messages": [], "tools": []})
        chunks = [chunk({"role": "assistant", "tool_calls": [{"index": 0, "id": "call1", "type": "function",
                     "function": {"name": "search", "arguments": '{"query":"\\u0073ecret-key"}'}}]}),
                  chunk({}, "tool_calls")]
        stream = record_model_stream(session, "r1", 1, chunks)
        self.assertEqual(next(stream).data["fragments"], {"tool:0:id": "call1"})
        self.assertEqual(next(stream).data["fragments"], {"tool:0:name": "search"})
        self.assertIsNone(next(stream).data["message"])
        with self.assertRaisesRegex(ValueError, "密钥"):
            next(stream)
        self.assertNotIn("u0073", session.path.read_text(encoding="utf-8"))

    def test_completed_model_stream_records_normalized_response(self):
        session = self.create()
        session.append_event("r1", "run.started", {"input": "x"})
        session.append_event("r1", "model.request", {"turn_index": 1, "model": "example", "messages": [], "tools": []})
        chunks = [chunk({"role": "assistant", "tool_calls": [{"index": 0, "id": "call1", "type": "function",
                                                              "function": {"name": "calculate", "arguments": "{}"}}]}),
                  chunk({}, "tool_calls")]
        events = list(record_model_stream(session, "r1", 1, chunks))
        self.assertEqual(events[-1].type, "model.response")
        self.assertEqual(events[-1].data["message"]["content"][0]["id"], "call1")
        self.assertEqual(session.trace("r1")[-1], events[-1])

    def test_incomplete_model_response_survives_recovery(self):
        session = self.create()
        session.append_event("r1", "run.started", {"input": "x"})
        session.append_event("r1", "model.request", {"turn_index": 1, "model": "example", "messages": [], "tools": []})
        stream = record_model_stream(session, "r1", 1, [chunk({"role": "assistant", "content": "partial"}),
                                                       chunk({}, "length")])
        self.assertEqual(next(stream).data["fragments"], {"text": "partial"})
        response = next(stream)
        self.assertEqual(response.data, {"turn_index": 1, "message": None, "finish_reason": "length", "usage": None})
        with self.assertRaises(ValueError):
            next(stream)
        reopened = self.open(session.id)
        self.assertEqual(reopened.trace("r1")[-1], response)
        self.assertEqual(reopened.recover()[0].data["status"], "interrupted")

    def test_incomplete_refusal_stream_persisted_before_error(self):
        session = self.create()
        session.append_event("r1", "run.started", {"input": "x"})
        session.append_event("r1", "model.request", {"turn_index": 1, "model": "example", "messages": [], "tools": []})
        stream = record_model_stream(session, "r1", 1, [chunk({"role": "assistant", "refusal": "safe refusal", "vendor": "ignored"}),
                                                       chunk({}, "length")])
        self.assertEqual(next(stream).data["fragments"], {"text": "safe refusal"})
        self.assertEqual(next(stream).data, {"turn_index": 1, "message": None, "finish_reason": "length", "usage": None})
        with self.assertRaises(ValueError):
            next(stream)
        reopened = self.open(session.id)
        self.assertEqual([event.type for event in reopened.trace("r1")],
                         ["run.started", "model.request", "model.delta", "model.response"])
        self.assertNotIn("vendor", session.path.read_text(encoding="utf-8"))
        self.assertEqual(reopened.recover()[0].data["status"], "interrupted")

    def test_incomplete_refusal_secret_not_persisted(self):
        session = self.create()
        session.append_event("r1", "run.started", {"input": "x"})
        session.append_event("r1", "model.request", {"turn_index": 1, "model": "example", "messages": [], "tools": []})
        stream = record_model_stream(session, "r1", 1, [chunk({"role": "assistant", "refusal": "safe "}),
                                                       chunk({"refusal": "secret-"}), chunk({"refusal": "key"}),
                                                       chunk({}, "length")])
        self.assertEqual(next(stream).data["fragments"], {"text": "safe "})
        self.assertIsNone(next(stream).data["message"])
        with self.assertRaises(ValueError):
            next(stream)
        self.assertNotIn("secret-", session.path.read_text(encoding="utf-8"))
        self.assertEqual([event.type for event in self.open(session.id).trace("r1")],
                         ["run.started", "model.request", "model.delta", "model.response"])

    def test_real_tool_result_recorded_after_security_check(self):
        session = create_session(self.root, secrets=('{"result": "3"}',))
        session.append_event("run", "run.started", {"input": "x"})
        arguments = '{"left":"1","operator":"+","right":"2"}'
        session.append_event("run", "tool.started", {"turn_index": 1, "tool_call_id": "call", "name": "calculate", "arguments": arguments})
        with self.assertRaisesRegex(ValueError, "密钥"):
            session.execute_tool(registry, "run", 1, "call", "calculate", arguments)
        self.assertEqual([event.type for event in session.trace("run")], ["run.started", "tool.started"])
        session = open_session(self.root, session.id, secrets=())
        self.assertEqual(session.execute_tool(registry, "run", 1, "call", "calculate", arguments), '{"result": "3"}')
        self.assertEqual(session.trace("run")[-1].type, "tool.completed")

    def test_escaped_tool_secrets_are_rejected_before_execution(self):
        session = create_session(self.root, secrets=("secret",))
        session.append_event("run", "run.started", {"input": "x"})
        arguments = '{"query":"\\u0073ecret"}'
        with self.assertRaisesRegex(ValueError, "密钥"):
            session.append_message(AssistantMessage(content=[ToolCallContent(id="call", name="search", arguments=arguments)]), "run")
        with self.assertRaisesRegex(ValueError, "密钥"):
            session.append_event("run", "tool.started", {"turn_index": 1, "tool_call_id": "call", "name": "search", "arguments": arguments})
        calls = []

        def profile(frame, event, arg):
            if event == "call" and frame.f_code is search.__code__:
                calls.append(True)

        previous = sys.getprofile()
        sys.setprofile(profile)
        try:
            with self.assertRaisesRegex(ValueError, "未启动"):
                session.execute_tool(registry, "run", 1, "call", "search", arguments)
        finally:
            sys.setprofile(previous)
        self.assertEqual(calls, [])
        self.assertEqual([event.type for event in session.trace("run")], ["run.started"])
        invalid = '{"query":"\\u0073ecret","extra":true}'
        with self.assertRaisesRegex(ValueError, "密钥"):
            session.append_event("run", "tool.started", {"turn_index": 1, "tool_call_id": "invalid", "name": "search", "arguments": invalid})
        arguments = '{"query":"退货"}'
        session.append_event("run", "tool.started", {"turn_index": 1, "tool_call_id": "safe", "name": "search", "arguments": arguments})
        self.assertIn("results", session.execute_tool(registry, "run", 1, "safe", "search", arguments))

    def test_request_limit(self):
        session = self.create()
        session.append_event("r1", "run.started", {"input": "x"})
        for turn in range(1, 65):
            session.append_event("r1", "model.request", {"turn_index": turn, "model": "example", "messages": [], "tools": []})
        with self.assertRaisesRegex(ValueError, "上限"):
            session.append_event("r1", "model.request", {"turn_index": 65, "model": "example", "messages": [], "tools": []})
        self.assertEqual(len(session.trace("r1")), 65)

    def test_invalid_version_rejected(self):
        session = self.create()
        header = session.path.read_text(encoding="utf-8")
        session.path.write_text(header.replace('"version":1', '"version":true'), encoding="utf-8")
        with self.assertRaises(ValueError):
            session.read()
        session.path.write_text(header.replace('"version":1,', ''), encoding="utf-8")
        with self.assertRaises(ValueError):
            session.read()

    def test_secret_rejection_and_confirmation(self):
        session = self.create()
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
