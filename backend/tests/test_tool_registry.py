import json
import unittest
from decimal import InvalidOperation

from pydantic import ValidationError

from src.agent.tools.calculator import Calculation, calculate
from src.agent.tools.registry import ToolDefinition, create_full_registry, declarations, execute, get, register, registry
from src.agent.tools.search import SearchQuery, search
from src.agent.tools.todo import TodoCommand, TodoList
from src.agent.tools.weather import WeatherQuery, weather


class ToolRegistryTests(unittest.TestCase):
    def test_registration_and_schema(self):
        expected = [("calculate", Calculation, calculate), ("weather", WeatherQuery, weather),
                    ("search", SearchQuery, search)]
        self.assertEqual([declaration["function"]["name"] for declaration in declarations(registry)],
                         [name for name, _, _ in expected])
        for declaration, (name, model, operation) in zip(declarations(registry), expected):
            tool = get(registry, name)
            self.assertIs(tool.parameters, model)
            self.assertIs(tool.execute, operation)
            self.assertEqual(declaration, {"type": "function", "function": {
                "name": name, "description": tool.description, "parameters": model.model_json_schema(),
            }})
        for name in ("todo", "missing"):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    get(registry, name)
                with self.assertRaises(ValueError):
                    execute(registry, name, '{}')
        isolated = {}
        register(isolated, get(registry, "calculate"))
        with self.assertRaises(ValueError):
            register(isolated, get(registry, "calculate"))
        for name in ("weather", "search"):
            self.assertIn("样例", get(registry, name).description)
            self.assertIn("非实时", get(registry, name).description)

    def test_validation_and_execution(self):
        self.assertEqual(json.loads(execute(registry, "calculate", ' {"left":"1","operator":"+","right":"2"} ')), {"result": "3"})
        for arguments in (
            "{", "[]", '{}',
            '{"left":"1","operator":"+"}',
            '{"left":"1","operator":"+","right":"2","extra":0}',
            '{"left":1,"operator":"+","right":"2"}',
            '{"left":"1","operator":"**","right":"2"}',
        ):
            with self.subTest(arguments=arguments), self.assertRaises(ValidationError):
                execute(registry, "calculate", arguments)
        with self.assertRaises(ZeroDivisionError):
            execute(registry, "calculate", '{"left":"1","operator":"/","right":"0"}')
        with self.assertRaises(ValueError):
            execute(registry, "calculate", '{"left":"NaN","operator":"+","right":"0"}')
        with self.assertRaises(InvalidOperation):
            execute(registry, "calculate", '{"left":"bad","operator":"+","right":"0"}')

    def test_weather_and_search_execution_and_validation(self):
        self.assertEqual(json.loads(execute(registry, "weather", '{"location":"广州"}')),
                         {"location": "广州", "condition": "晴", "temperature_c": 24,
                          "source": "custom", "is_live": False})
        result = json.loads(execute(registry, "search", '{"query":"退货","limit":1}'))
        self.assertEqual(result["source"], "mock")
        self.assertEqual(len(result["results"]), 1)
        for name, arguments in (("weather", '{}'), ("weather", '{"location":1}'),
                                ("search", '{}'), ("search", '{"query":"退货","limit":11}')):
            with self.subTest(name=name, arguments=arguments), self.assertRaises(ValidationError):
                execute(registry, name, arguments)
        for name, arguments in (("weather", '{"location":"未知地点"}'),
                                ("search", '{"query":"   "}')):
            with self.subTest(name=name), self.assertRaises(ValueError):
                execute(registry, name, arguments)

    def test_full_registries_bind_independent_todo_lists(self):
        stores = [TodoList(), TodoList()]
        registries = [create_full_registry(store) for store in stores]
        for tools, store in zip(registries, stores):
            self.assertIsNot(tools, registry)
            self.assertEqual([d["function"]["name"] for d in declarations(tools)],
                             ["calculate", "weather", "search", "todo"])
            for declaration, model in zip(declarations(tools), (Calculation, WeatherQuery, SearchQuery, TodoCommand)):
                self.assertEqual(declaration["function"]["parameters"], model.model_json_schema())
            self.assertIs(get(tools, "todo").execute.args[0], store)
        self.assertEqual([d["function"]["name"] for d in declarations(registry)],
                         ["calculate", "weather", "search"])
        first, second = registries
        self.assertEqual(json.loads(execute(first, "todo", '{"action":"add","title":"甲"}'))["item"]["id"], 1)
        self.assertEqual(json.loads(execute(second, "todo", '{"action":"add","title":"乙"}'))["item"]["id"], 1)
        changed = json.loads(execute(first, "todo", '{"action":"set_status","id":1,"status":"completed"}'))["item"]
        self.assertEqual(changed, {"id": 1, "title": "甲", "status": "completed"})
        self.assertEqual(json.loads(execute(first, "todo", '{"action":"list"}'))["items"], [changed])
        self.assertEqual(json.loads(execute(first, "todo", '{"action":"delete","id":1}'))["deleted"], changed)
        self.assertEqual(json.loads(execute(first, "todo", '{"action":"list"}'))["items"], [])
        self.assertEqual(json.loads(execute(second, "todo", '{"action":"list"}'))["items"],
                         [{"id": 1, "title": "乙", "status": "pending"}])
        with self.assertRaises(ValidationError):
            execute(first, "todo", '{"action":"set_status","id":"1"}')
        with self.assertRaises(ValueError):
            execute(first, "todo", '{"action":"delete","id":1}')

    def test_todo_confirmation_uses_shared_snapshot(self):
        store = TodoList()
        tools = create_full_registry(store, lambda: [{"id": 7, "title": "共享任务", "status": "pending"}])
        confirmation = get(tools, "todo").confirmation
        self.assertIsNotNone(confirmation)
        self.assertIsNone(confirmation(TodoCommand(action="list")))
        self.assertEqual(confirmation(TodoCommand(action="delete", id=7)), {
            "name": "todo", "action": "delete", "task": {"id": 7, "title": "共享任务"},
        })
        with self.assertRaises(ValueError):
            confirmation(TodoCommand(action="delete", id=8))

    def test_non_string_result_fails_after_validation(self):
        calls = []

        def operation(parameters):
            calls.append(parameters)
            return 42

        isolated = {}
        register(isolated, ToolDefinition("bad", "", Calculation, operation))
        with self.assertRaises(ValidationError):
            execute(isolated, "bad", "{}")
        self.assertEqual(calls, [])
        with self.assertRaises(TypeError):
            execute(isolated, "bad", '{"left":"1","operator":"+","right":"2"}')
        self.assertIsInstance(calls[0], Calculation)


if __name__ == "__main__":
    unittest.main()
