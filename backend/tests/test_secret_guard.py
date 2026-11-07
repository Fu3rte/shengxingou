import unittest

from src.agent.security import StreamSecretGuard, check, create_guard, feed, finish
from src.agent.types import UserMessage


class SecretGuardTests(unittest.TestCase):
    def test_nested_inputs_and_tool_content(self):
        guard = create_guard(["a\"b", "token-value"])
        self.assertNotIn("token-value", repr(guard))
        for value in ("a\"b", {"text": ["before token-value after"]}, {"token-value": "value"}, UserMessage(content="token-value")):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "密钥"):
                check(guard, value)
        check(guard, {"arguments": '{"title":"safe"}', "result": "ok"})
        check(create_guard([""]), "safe")

    def test_split_secret_never_released(self):
        guard = StreamSecretGuard(create_guard(["secret"]))
        self.assertEqual(feed(guard, {"content": "hello "}), [{"content": "hello "}])
        self.assertEqual(feed(guard, {"content": "se"}), [])
        self.assertEqual(feed(guard, {"arguments:0": "safe"}), [])
        with self.assertRaisesRegex(ValueError, "密钥"):
            feed(guard, {"content": "cret"})
        with self.assertRaises(ValueError):
            finish(guard)

    def test_tool_arguments_checked_independently_and_order_preserved(self):
        guard = StreamSecretGuard(create_guard(["abcdef"]))
        self.assertEqual(feed(guard, {"arguments:0": "ab"}), [])
        self.assertEqual(feed(guard, {"arguments:1": "cdef"}), [])
        self.assertEqual(feed(guard, {"arguments:0": "X"}), [
            {"arguments:0": "ab"}, {"arguments:1": "cdef"}, {"arguments:0": "X"},
        ])
        self.assertEqual(feed(guard, {"arguments:0": "abcd"}), [])
        with self.assertRaisesRegex(ValueError, "密钥"):
            feed(guard, {"arguments:0": "ef"})

    def test_finish_releases_incomplete_prefix(self):
        guard = StreamSecretGuard(create_guard(["secret"]))
        self.assertEqual(feed(guard, {"content": "se"}), [])
        self.assertEqual(finish(guard), [{"content": "se"}])
        with self.assertRaises(ValueError):
            feed(guard, {"content": "cret"})


if __name__ == "__main__":
    unittest.main()
