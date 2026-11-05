import json
import unittest

from pydantic import ValidationError

from src.agent.tools.weather import weather


class WeatherTests(unittest.TestCase):
    def test_known_location(self):
        self.assertEqual(
            json.loads(weather('{"location": "示例城"}')),
            {"location": "示例城", "condition": "晴", "temperature_c": 24, "source": "custom", "is_live": False},
        )

    def test_location_is_stripped(self):
        self.assertEqual(json.loads(weather('{"location": " 示例城 "}'))["location"], "示例城")

    def test_unknown_location_fails(self):
        with self.assertRaises(ValueError):
            weather('{"location": "不存在的城"}')

    def test_invalid_arguments_fail(self):
        with self.assertRaises(ValidationError):
            weather('{"location": ""}')
        with self.assertRaises(ValueError):
            weather('{"location": "   "}')
        with self.assertRaises(ValidationError):
            weather('{"location": "示例城", "unit": "F"}')


if __name__ == "__main__":
    unittest.main()
