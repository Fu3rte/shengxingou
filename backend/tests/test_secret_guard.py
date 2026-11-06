import unittest

from src.agent.security import SecretGuard, StreamSecretGuard
from src.agent.types import UserMessage


class SecretGuardTests(unittest.TestCase):
    def test_nested_inputs_and_tool_content(self):
        guard = SecretGuard(["a\"b", "token-value"])
        for value in ("a\"b", {"text": ["before token-value after"]}, {"token-value": "value"}, UserMessage(content="token-value")):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "密钥"):
                guard.check(value)
        guard.check({"arguments": '{"title":"safe"}', "result": "ok"})

    def test_split_secret_never_released(self):
        guard = StreamSecretGuard(["secret"])
        self.assertEqual(guard.feed({"content": "hello "}), [{"content": "hello "}])
        self.assertEqual(guard.feed({"content": "se"}), [])
        self.assertEqual(guard.feed({"arguments:0": "safe"}), [])
        with self.assertRaisesRegex(ValueError, "密钥"):
            guard.feed({"content": "cret"})
        with self.assertRaises(ValueError):
            guard.finish()

    def test_tool_arguments_checked_independently_and_order_preserved(self):
        guard = StreamSecretGuard(["abcdef"])
        self.assertEqual(guard.feed({"arguments:0": "ab"}), [])
        self.assertEqual(guard.feed({"arguments:1": "cdef"}), [])
        self.assertEqual(guard.feed({"arguments:0": "X"}), [
            {"arguments:0": "ab"}, {"arguments:1": "cdef"}, {"arguments:0": "X"},
        ])
        self.assertEqual(guard.feed({"arguments:0": "abcd"}), [])
        with self.assertRaisesRegex(ValueError, "密钥"):
            guard.feed({"arguments:0": "ef"})

    def test_finish_releases_incomplete_prefix(self):
        guard = StreamSecretGuard(["secret"])
        self.assertEqual(guard.feed({"content": "se"}), [])
        self.assertEqual(guard.finish(), [{"content": "se"}])
        with self.assertRaises(ValueError):
            guard.feed({"content": "cret"})


if __name__ == "__main__":
    unittest.main()
