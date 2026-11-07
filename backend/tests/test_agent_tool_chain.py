import json
import unittest

from pydantic import ValidationError

from src.agent.tools.registry import create_full_registry, declarations, execute
from src.agent.tools.todo import TodoList


class AgentToolChainTests(unittest.TestCase):
    def test_real_tools_and_arguments(self):
        registry = create_full_registry(TodoList())
        self.assertEqual([tool['function']['name'] for tool in declarations(registry)],
                         ['calculate', 'weather', 'search', 'todo'])
        self.assertEqual(json.loads(execute(registry, 'calculate', ' {"left":"7", "operator":"+", "right":"3"} ')), {'result': '10'})
        self.assertFalse(json.loads(execute(registry, 'weather', '{"location":"广州"}'))['is_live'])
        self.assertEqual(json.loads(execute(registry, 'search', '{"query":"python"}'))['source'], 'mock')
        self.assertEqual(json.loads(execute(registry, 'todo', '{"action":"list"}')), {'items': []})

    def test_tool_failures_propagate(self):
        registry = create_full_registry(TodoList())
        with self.assertRaises(ValidationError):
            execute(registry, 'calculate', '{}')
        with self.assertRaises(ValueError):
            execute(registry, 'unknown', '{}')
        with self.assertRaises(ZeroDivisionError):
            execute(registry, 'calculate', '{"left":"1","operator":"/","right":"0"}')


if __name__ == '__main__':
    unittest.main()
