import json
import unittest
from decimal import InvalidOperation

from pydantic import ValidationError

from src.agent.tools.calculator import Calculation, calculate
from src.agent.tools.registry import ToolDefinition, ToolRegistry, create_full_registry, registry
from src.agent.tools.search import SearchQuery, search
from src.agent.tools.todo import TodoCommand, TodoList
from src.agent.tools.weather import WeatherQuery, weather


class ToolRegistryTests(unittest.TestCase):
    def test_registration_and_schema(self):
        expected = [("calculate", Calculation, calculate), ("weather", WeatherQuery, weather),
                    ("search", SearchQuery, search)]
        self.assertEqual([declaration["function"]["name"] for declaration in registry.declarations()],
                         [name for name, _, _ in expected])
        for declaration, (name, model, execute) in zip(registry.declarations(), expected):
            tool = registry.get(name)
            self.assertIs(tool.parameters, model)
            self.assertIs(tool.execute, execute)
            self.assertEqual(declaration, {"type": "function", "function": {
                "name": name, "description": tool.description, "parameters": model.model_json_schema(),
            }})
        for name in ("todo", "missing"):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    registry.get(name)
                with self.assertRaises(ValueError):
                    registry.execute(name, '{}')
        isolated = ToolRegistry()
        isolated.register(registry.get("calculate"))
        with self.assertRaises(ValueError):
            isolated.register(registry.get("calculate"))
        for name in ("weather", "search"):
            self.assertIn("样例", registry.get(name).description)
            self.assertIn("非实时", registry.get(name).description)

    def test_validation_and_execution(self):
        self.assertEqual(json.loads(registry.execute("calculate", ' {"left":"1","operator":"+","right":"2"} ')), {"result": "3"})
        for arguments in (
            "{", "[]", '{}',
            '{"left":"1","operator":"+"}',
            '{"left":"1","operator":"+","right":"2","extra":0}',
            '{"left":1,"operator":"+","right":"2"}',
            '{"left":"1","operator":"**","right":"2"}',
        ):
            with self.subTest(arguments=arguments), self.assertRaises(ValidationError):
                registry.execute("calculate", arguments)
        with self.assertRaises(ZeroDivisionError):
            registry.execute("calculate", '{"left":"1","operator":"/","right":"0"}')
        with self.assertRaises(ValueError):
            registry.execute("calculate", '{"left":"NaN","operator":"+","right":"0"}')
        with self.assertRaises(InvalidOperation):
            registry.execute("calculate", '{"left":"bad","operator":"+","right":"0"}')

    def test_weather_and_search_execution_and_validation(self):
        self.assertEqual(json.loads(registry.execute("weather", '{"location":"示例城"}')),
                         {"location": "示例城", "condition": "晴", "temperature_c": 24,
                          "source": "custom", "is_live": False})
        result = json.loads(registry.execute("search", '{"query":"退货","limit":1}'))
        self.assertEqual(result["source"], "mock")
        self.assertEqual(len(result["results"]), 1)
        for name, arguments in (("weather", '{}'), ("weather", '{"location":1}'),
                                ("search", '{}'), ("search", '{"query":"退货","limit":11}')):
            with self.subTest(name=name, arguments=arguments), self.assertRaises(ValidationError):
                registry.execute(name, arguments)
        for name, arguments in (("weather", '{"location":"未知地点"}'),
                                ("search", '{"query":"   "}')):
            with self.subTest(name=name), self.assertRaises(ValueError):
                registry.execute(name, arguments)

    def test_full_registries_bind_independent_todo_lists(self):
        stores = [TodoList(), TodoList()]
        registries = [create_full_registry(store) for store in stores]
        for tools, store in zip(registries, stores):
            self.assertIsNot(tools, registry)
            self.assertEqual([d["function"]["name"] for d in tools.declarations()],
                             ["calculate", "weather", "search", "todo"])
            for declaration, model in zip(tools.declarations(), (Calculation, WeatherQuery, SearchQuery, TodoCommand)):
                self.assertEqual(declaration["function"]["parameters"], model.model_json_schema())
            self.assertIs(tools.get("todo").execute.__self__, store)
        self.assertEqual([d["function"]["name"] for d in registry.declarations()],
                         ["calculate", "weather", "search"])
        first, second = registries
        self.assertEqual(json.loads(first.execute("todo", '{"action":"add","title":"甲"}'))["item"]["id"], 1)
        self.assertEqual(json.loads(second.execute("todo", '{"action":"add","title":"乙"}'))["item"]["id"], 1)
        changed = json.loads(first.execute("todo", '{"action":"set_status","id":1,"status":"completed"}'))["item"]
        self.assertEqual(changed, {"id": 1, "title": "甲", "status": "completed"})
        self.assertEqual(json.loads(first.execute("todo", '{"action":"list"}'))["items"], [changed])
        self.assertEqual(json.loads(first.execute("todo", '{"action":"delete","id":1}'))["deleted"], changed)
        self.assertEqual(json.loads(first.execute("todo", '{"action":"list"}'))["items"], [])
        self.assertEqual(json.loads(second.execute("todo", '{"action":"list"}'))["items"],
                         [{"id": 1, "title": "乙", "status": "pending"}])
        with self.assertRaises(ValidationError):
            first.execute("todo", '{"action":"set_status","id":"1"}')
        with self.assertRaises(ValueError):
            first.execute("todo", '{"action":"delete","id":1}')

    def test_non_string_result_fails_after_validation(self):
        calls = []

        def execute(parameters):
            calls.append(parameters)
            return 42

        isolated = ToolRegistry()
        isolated.register(ToolDefinition("bad", "", Calculation, execute))
        with self.assertRaises(ValidationError):
            isolated.execute("bad", "{}")
        self.assertEqual(calls, [])
        with self.assertRaises(TypeError):
            isolated.execute("bad", '{"left":"1","operator":"+","right":"2"}')
        self.assertIsInstance(calls[0], Calculation)


if __name__ == "__main__":
    unittest.main()
