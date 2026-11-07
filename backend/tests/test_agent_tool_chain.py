import json
import unittest

from pydantic import ValidationError

from src.agent.tools.registry import create_full_registry
from src.agent.tools.todo import TodoList


class AgentToolChainTests(unittest.TestCase):
    def test_real_tools_and_arguments(self):
        registry = create_full_registry(TodoList())
        self.assertEqual([tool['function']['name'] for tool in registry.declarations()],
                         ['calculate', 'weather', 'search', 'todo'])
        self.assertEqual(json.loads(registry.execute('calculate', ' {"left":"7", "operator":"+", "right":"3"} ')), {'result': '10'})
        self.assertFalse(json.loads(registry.execute('weather', '{"location":"示例城"}'))['is_live'])
        self.assertEqual(json.loads(registry.execute('search', '{"query":"python"}'))['source'], 'mock')
        self.assertEqual(json.loads(registry.execute('todo', '{"action":"list"}')), {'items': []})

    def test_tool_failures_propagate(self):
        registry = create_full_registry(TodoList())
        with self.assertRaises(ValidationError):
            registry.execute('calculate', '{}')
        with self.assertRaises(ValueError):
            registry.execute('unknown', '{}')
        with self.assertRaises(ZeroDivisionError):
            registry.execute('calculate', '{"left":"1","operator":"/","right":"0"}')


if __name__ == '__main__':
    unittest.main()
