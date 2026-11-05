import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Thread

from openai import OpenAI
from pydantic import ValidationError

from src import server
from src.agent.agent_loop import agent_loop
from src.agent.tools.registry import registry


ARGUMENTS = ' {"left":"7", "operator":"+", "right":"3"} '


class CompletionHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append(request)
        messages = request["messages"]
        if messages[-1]["role"] == "tool":
            message = {"role": "assistant", "content": "完成"}
            finish = "stop"
        else:
            prompt = messages[-1]["content"]
            name = {"unknown": "missing", "todo": "todo"}.get(prompt, "calculate")
            arguments = "{" if prompt == "invalid" else ARGUMENTS
            if prompt == "divide":
                arguments = '{"left":"1","operator":"/","right":"0"}'
            if prompt == "todo":
                arguments = '{"action":"list"}'
            calls = [{"id": "call_1", "type": "function", "function": {"name": name, "arguments": arguments}}]
            if prompt == "two":
                calls.append({"id": "call_2", "type": "function", "function": {"name": name, "arguments": arguments}})
            if prompt == "mixed":
                calls.extend([
                    {"id": "call_2", "type": "function", "function": {"name": "weather", "arguments": '{"location":"示例城"}'}},
                    {"id": "call_3", "type": "function", "function": {"name": "search", "arguments": '{"query":"退货","limit":1}'}},
                ])
            message = {"role": "assistant", "content": None, "tool_calls": calls}
            finish = "tool_calls"
        body = json.dumps({"id": "completion", "object": "chat.completion", "created": 1, "model": "local",
                           "choices": [{"index": 0, "finish_reason": finish, "message": message}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class AgentToolChainTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.http = ThreadingHTTPServer(("127.0.0.1", 0), CompletionHandler)
        cls.http.requests = []
        cls.thread = Thread(target=cls.http.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.http.server_port}/v1"

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown()
        cls.http.server_close()
        cls.thread.join()

    def setUp(self):
        self.http.requests.clear()

    def test_loop_runs_tools_in_order_and_preserves_ids(self):
        messages = [{"role": "user", "content": "two"}]
        updates = []
        with OpenAI(api_key="local", base_url=self.url, max_retries=0) as client:
            result = agent_loop(client, "local", messages, on_update=lambda: updates.append(len(messages)))
        self.assertEqual(result, "完成")
        self.assertEqual(self.http.requests[0]["tools"], registry.declarations())
        self.assertEqual(messages[1]["tool_calls"][0]["function"]["arguments"], ARGUMENTS)
        self.assertEqual([message["tool_call_id"] for message in messages[2:4]], ["call_1", "call_2"])
        self.assertEqual([json.loads(message["content"]) for message in messages[2:4]], [{"result": "10"}] * 2)
        self.assertEqual(updates, [3, 4])
        self.assertEqual(self.http.requests[1]["messages"], messages[:-1])

    def test_loop_runs_three_real_tools_in_order(self):
        messages = [{"role": "user", "content": "mixed"}]
        updates = []
        with OpenAI(api_key="local", base_url=self.url, max_retries=0) as client:
            self.assertEqual(agent_loop(client, "local", messages, on_update=lambda: updates.append(len(messages))), "完成")
        self.assertEqual([d["function"]["name"] for d in self.http.requests[0]["tools"]],
                         ["calculate", "weather", "search"])
        self.assertEqual([message["tool_call_id"] for message in messages[2:5]], ["call_1", "call_2", "call_3"])
        self.assertEqual(json.loads(messages[2]["content"]), {"result": "10"})
        self.assertEqual(json.loads(messages[3]["content"])["is_live"], False)
        self.assertEqual(json.loads(messages[4]["content"])["source"], "mock")
        self.assertEqual(updates, [3, 4, 5])
        self.assertEqual(self.http.requests[1]["messages"], messages[:-1])

    def test_stop_after_tool_keeps_result_and_skips_next(self):
        messages = [{"role": "user", "content": "two"}]
        stop = Event()
        with OpenAI(api_key="local", base_url=self.url, max_retries=0) as client:
            self.assertIsNone(agent_loop(client, "local", messages, stop=stop, on_update=stop.set))
        self.assertEqual(len(self.http.requests), 1)
        self.assertEqual([message["tool_call_id"] for message in messages if message["role"] == "tool"], ["call_1"])
        self.assertEqual(json.loads(messages[-1]["content"]), {"result": "10"})

    def test_failure_does_not_append_tool_result(self):
        for prompt, error in (("invalid", ValidationError), ("unknown", ValueError), ("todo", ValueError),
                              ("divide", ZeroDivisionError)):
            messages = [{"role": "user", "content": prompt}]
            updates = []
            with self.subTest(prompt=prompt), OpenAI(api_key="local", base_url=self.url, max_retries=0) as client:
                with self.assertRaises(error):
                    agent_loop(client, "local", messages, on_update=lambda: updates.append(True))
            self.assertEqual([message["role"] for message in messages], ["user", "assistant"])
            self.assertEqual(updates, [])

    def test_cli_entrypoint(self):
        env = os.environ.copy()
        env.update(OPENAI_MODEL="local", OPENAI_BASE_URL=self.url, OPENAI_API_KEY="local")
        result = subprocess.run([sys.executable, "-m", "src.agent.agent_loop"], input="mixed\n", text=True,
                                capture_output=True, env=env, timeout=20, cwd=Path(__file__).resolve().parents[1])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"result": "10"', result.stdout)
        self.assertIn("完成", result.stdout)
        self.assertIn('"is_live": false', result.stdout)
        self.assertIn('"source": "mock"', result.stdout)
        self.assertEqual(self.http.requests[0]["tools"], registry.declarations())

    def test_server_turn(self):
        directory = Path(__file__).resolve().parents[2] / ".tool-tmp"
        directory.mkdir(exist_ok=True)
        previous_data, previous_config = server.DATA, server.CONFIG
        with tempfile.TemporaryDirectory(dir=directory) as folder:
            try:
                server.DATA = Path(folder) / "turns.json"
                server.CONFIG = Path(folder) / "config.json"
                server.save_config({"model": "local", "base_url": self.url, "api_key": "test-secret-key"})
                turn = server.start_turn("mixed")
                deadline = time.monotonic() + 20
                while True:
                    with server.lock:
                        running = turn["id"] in server.active
                    if not running:
                        break
                    if time.monotonic() >= deadline:
                        self.fail("server turn timed out")
                    time.sleep(0.01)
                saved = server.read_turns()[0]
                self.assertEqual(saved["status"], "completed")
                self.assertNotIn("test-secret-key", json.dumps(saved))
                self.assertEqual([message["role"] for message in saved["messages"]],
                                 ["user", "assistant", "tool", "tool", "tool", "assistant"])
                self.assertEqual([message["tool_call_id"] for message in saved["messages"][2:5]],
                                 ["call_1", "call_2", "call_3"])
                self.assertEqual(json.loads(saved["messages"][2]["content"]), {"result": "10"})
                self.assertEqual(json.loads(saved["messages"][3]["content"])["source"], "custom")
                self.assertEqual(json.loads(saved["messages"][4]["content"])["source"], "mock")
                self.assertEqual(self.http.requests[0]["tools"], registry.declarations())
            finally:
                server.DATA, server.CONFIG = previous_data, previous_config


if __name__ == "__main__":
    unittest.main()
