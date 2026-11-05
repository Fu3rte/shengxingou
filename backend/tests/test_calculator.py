import json
import unittest
from decimal import InvalidOperation

from pydantic import ValidationError

from src.agent.tools.calculator import Calculation, calculate


class CalculatorTests(unittest.TestCase):
    def test_operations(self):
        for operation, expected in (("+", "10"), ("-", "4"), ("*", "21"), ("/", "2.333333333333333333333333333")):
            with self.subTest(operation=operation):
                self.assertEqual(json.loads(calculate(Calculation(left="7", operator=operation, right="3")))["result"], expected)
        self.assertEqual(json.loads(calculate(Calculation(left="0.1", operator="+", right="0.2")))["result"], "0.3")

    def test_invalid_arguments_fail(self):
        for arguments in (
            '{"left": "1", "operator": "**", "right": "2"}',
            '{"left": 1, "operator": "+", "right": "2"}',
            '{"left": "1", "operator": "+", "right": "2", "extra": 3}',
        ):
            with self.subTest(arguments=arguments), self.assertRaises(ValidationError):
                Calculation.model_validate_json(arguments)

    def test_invalid_numbers_fail(self):
        for value in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                calculate(Calculation(left=value, operator="+", right="2"))
        with self.assertRaises(InvalidOperation):
            calculate(Calculation(left="invalid", operator="+", right="2"))
        with self.assertRaises(ZeroDivisionError):
            calculate(Calculation(left="1", operator="/", right="0"))


if __name__ == "__main__":
    unittest.main()
