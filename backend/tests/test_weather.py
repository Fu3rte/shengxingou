import json
import unittest

from pydantic import ValidationError

from src.agent.tools.weather import WeatherQuery, weather


class WeatherTests(unittest.TestCase):
    def test_known_location(self):
        self.assertEqual(
            json.loads(weather(WeatherQuery(location="广州"))),
            {"location": "广州", "condition": "晴", "temperature_c": 24, "source": "custom", "is_live": False},
        )

    def test_location_is_stripped(self):
        self.assertEqual(json.loads(weather(WeatherQuery(location=" 广州 ")))["location"], "广州")

    def test_unknown_location_fails(self):
        with self.assertRaises(ValueError):
            weather(WeatherQuery(location="不存在的城"))

    def test_invalid_arguments_fail(self):
        with self.assertRaises(ValidationError):
            WeatherQuery(location="")
        with self.assertRaises(ValueError):
            weather(WeatherQuery(location="   "))
        with self.assertRaises(ValidationError):
            WeatherQuery.model_validate_json('{"location": "广州", "unit": "F"}')


if __name__ == "__main__":
    unittest.main()
