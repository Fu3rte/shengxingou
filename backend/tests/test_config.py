import os
import json
import tempfile
import unittest
from pathlib import Path

from fastapi import HTTPException

from src import api as server


class ConfigTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = Path(__file__).resolve().parents[2] / ".tool-tmp"
        directory.mkdir(exist_ok=True)
        self.folder = tempfile.TemporaryDirectory(dir=directory)
        self.previous = server.CONFIG
        self.environ = {key: os.environ.get(key) for key in ("OPENAI_MODEL", "OPENAI_BASE_URL", "OPENAI_API_KEY")}
        server.CONFIG = Path(self.folder.name) / "config.json"
        os.environ["OPENAI_MODEL"] = "env-model"
        os.environ["OPENAI_BASE_URL"] = "https://env.example/v1"
        os.environ["OPENAI_API_KEY"] = "env-key-1234567890"

    def tearDown(self):
        server.CONFIG = self.previous
        for key, value in self.environ.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.folder.cleanup()

    def save_config(self, data):
        server.CONFIG.write_text(json.dumps(data), encoding="utf-8")

    def test_empty_config_falls_back_to_environment(self):
        self.assertEqual(server.resolve_config()["model"], "env-model")
        self.assertEqual(server.resolve_config()["base_url"], "https://env.example/v1")

    def test_saved_config_overrides_environment_and_masks_key(self):
        self.save_config({"model": "file-model", "base_url": "https://file.example/v1", "api_key": "sk-file-abcdefghijk"})
        current = server.resolve_config()
        self.assertEqual(current["model"], "file-model")
        self.assertEqual(current["api_key"], "sk-file-abcdefghijk")
        public = server.public_config()
        self.assertEqual(public["model"], "file-model")
        self.assertTrue(public["api_key_set"])
        self.assertNotIn("abcdefghijk", public["api_key"])

    async def test_put_config_persists_and_keeps_existing_key(self):
        self.save_config({"model": "old", "base_url": "", "api_key": "sk-keep-000011112222"})
        result = await server.put_config(server.ConfigInput(model="new-model", base_url="https://new.example/v1", api_key=""))
        self.assertEqual(result["model"], "new-model")
        self.assertTrue(result["api_key_set"])
        self.assertEqual(server.read_config()["api_key"], "sk-keep-000011112222")

    async def test_reasoning_field_config(self):
        self.assertEqual(server.resolve_config()["reasoning_field"], "reasoning_content")
        result = await server.put_config(server.ConfigInput(model="m", reasoning_field="thought"))
        self.assertEqual(result["reasoning_field"], "thought")
        self.assertEqual(server.read_config()["reasoning_field"], "thought")
        for field in ("content", "tool_calls", "delta.thought", " "):
            with self.subTest(field=field), self.assertRaises(HTTPException):
                await server.put_config(server.ConfigInput(model="m", reasoning_field=field))

    async def test_put_config_rejects_invalid_base_url(self):
        with self.assertRaises(HTTPException) as raised:
            await server.put_config(server.ConfigInput(model="m", base_url="ftp://x", api_key=""))
        self.assertEqual(raised.exception.status_code, 422)

    async def test_put_config_rejects_blank_model(self):
        with self.assertRaises(HTTPException) as raised:
            await server.put_config(server.ConfigInput(model="  ", base_url="", api_key=""))
        self.assertEqual(raised.exception.status_code, 422)


if __name__ == "__main__":
    unittest.main()
