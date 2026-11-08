import base64
import json
import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from src.agent.session import create_session
from src.agent.tools.registry import create_registry, execute
from src.agent.tools.todo import TodoList
from src.agent.types import AssistantMessage, ToolCallContent


registry = create_registry(TodoList())


class FileToolsTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[2] / ".tool-tmp"
        root.mkdir(exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "file"

    def call(self, name, **arguments):
        return execute(registry, name, json.dumps(arguments, ensure_ascii=False))

    def test_text_append_overwrite_and_stop(self):
        self.assertEqual(self.call("write", path=str(self.path), content="你好", mode="overwrite"), "6")
        self.assertEqual(self.call("write", path=str(self.path), content="世界", mode="append"), "6")
        self.assertEqual(self.call("read", path=str(self.path)), "你好世界")
        self.assertEqual(self.call("write", path=str(self.path), content="新", mode="overwrite"), "3")
        arguments = json.dumps({"path": str(self.path), "content": "bad", "mode": "overwrite"})
        self.assertIsNone(execute(registry, "write", arguments, start=lambda: False))
        self.assertEqual(self.call("read", path=str(self.path)), "新")

    def test_binary_and_validation(self):
        binary = b"\x00\xff\x80"
        encoded = base64.b64encode(binary).decode("ascii")
        self.assertEqual(self.call("write", path=str(self.path), content=encoded, mode="overwrite", encoding="base64"), "3")
        self.assertEqual(self.path.read_bytes(), binary)
        self.assertEqual(self.call("read", path=str(self.path), encoding="base64"), encoded)
        with self.assertRaises(UnicodeDecodeError):
            self.call("read", path=str(self.path))
        with self.assertRaises(ValidationError):
            self.call("write", path=str(self.path), content="bad", mode="invalid")
        self.assertEqual(self.path.read_bytes(), binary)

    def test_encoded_secrets_are_rejected(self):
        secret = "secret-key"
        encoded = base64.b64encode(secret.encode()).decode("ascii")
        arguments = json.dumps({"path": str(self.path), "content": encoded, "mode": "overwrite", "encoding": "base64"})
        with self.assertRaisesRegex(ValueError, "密钥"):
            execute(registry, "write", arguments, secrets=(secret,))
        self.assertFalse(self.path.exists())
        self.path.write_bytes(secret.encode())
        arguments = json.dumps({"path": str(self.path), "encoding": "base64"})
        with self.assertRaisesRegex(ValueError, "密钥"):
            execute(registry, "read", arguments, secrets=(secret,))

    def test_encoded_secret_is_rejected_before_recording(self):
        secret = "secret-key"
        session = create_session(Path(self.directory.name) / "sessions", secrets=(secret,))
        session.append_event("run", "run.started", {"input": "safe"})
        arguments = json.dumps({
            "path": str(self.path), "content": base64.b64encode(secret.encode()).decode(),
            "mode": "overwrite", "encoding": "base64",
        })
        with self.assertRaisesRegex(ValueError, "密钥"):
            session.append_event("run", "tool.started", {
                "turn_index": 1, "tool_call_id": "call", "name": "write", "arguments": arguments,
            })
        with self.assertRaisesRegex(ValueError, "密钥"):
            session.append_message(AssistantMessage(content=[
                ToolCallContent(id="call", name="write", arguments=arguments),
            ]), "run")
        self.assertNotIn(base64.b64encode(secret.encode()), session.path.read_bytes())


if __name__ == "__main__":
    unittest.main()
