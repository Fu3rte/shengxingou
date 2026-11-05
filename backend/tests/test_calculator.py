import json
import unittest

from pydantic import ValidationError

from src.agent.tools.calculator import calculate


class CalculatorTests(unittest.TestCase):
    def test_operations(self):
        for operation, expected in (("+", "10"), ("-", "4"), ("*", "21"), ("/", "2.333333333333333333333333333")):
            with self.subTest(operation=operation):
                arguments = json.dumps({"left": "7", "operator": operation, "right": "3"})
                self.assertEqual(json.loads(calculate(arguments))["result"], expected)
        self.assertEqual(json.loads(calculate('{"left":"0.1","operator":"+","right":"0.2"}'))["result"], "0.3")

    def test_invalid_arguments_fail(self):
        for arguments in (
            '{"left": "1", "operator": "**", "right": "2"}',
            '{"left": 1, "operator": "+", "right": "2"}',
            '{"left": "1", "operator": "+", "right": "2", "extra": 3}',
        ):
            with self.subTest(arguments=arguments), self.assertRaises(ValidationError):
                calculate(arguments)

    def test_invalid_numbers_fail(self):
        with self.assertRaises(ValueError):
            calculate('{"left": "NaN", "operator": "+", "right": "2"}')
        with self.assertRaises(ZeroDivisionError):
            calculate('{"left": "1", "operator": "/", "right": "0"}')


if __name__ == "__main__":
    unittest.main()
