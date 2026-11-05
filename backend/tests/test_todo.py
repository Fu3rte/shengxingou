import json
import unittest
from threading import Barrier, Thread

from pydantic import ValidationError

from src.agent.tools.todo import TodoList, todo


class TodoTests(unittest.TestCase):
    def setUp(self):
        self.store = TodoList()

    def test_add_assigns_increasing_ids(self):
        first = json.loads(todo('{"action": "add", "title": "整理资料"}', self.store))["item"]
        second = json.loads(todo('{"action": "add", "title": "写周报"}', self.store))["item"]
        self.assertEqual(first, {"id": 1, "title": "整理资料", "status": "pending"})
        self.assertEqual(second, {"id": 2, "title": "写周报", "status": "pending"})
        self.assertEqual([item["id"] for item in json.loads(todo('{"action": "list"}', self.store))["items"]], [1, 2])

    def test_list_returns_snapshot(self):
        todo('{"action": "add", "title": "整理资料"}', self.store)
        items = json.loads(todo('{"action": "list"}', self.store))["items"]
        items[0]["title"] = "篡改"
        items.append({"id": 99})
        self.assertEqual(
            json.loads(todo('{"action": "list"}', self.store))["items"],
            [{"id": 1, "title": "整理资料", "status": "pending"}],
        )

    def test_set_status_is_repeatable(self):
        todo('{"action": "add", "title": "整理资料"}', self.store)
        first = json.loads(todo('{"action": "set_status", "id": 1, "status": "completed"}', self.store))
        second = json.loads(todo('{"action": "set_status", "id": 1, "status": "completed"}', self.store))
        self.assertEqual(first, {"item": {"id": 1, "title": "整理资料", "status": "completed"}})
        self.assertEqual(first, second)
        self.assertEqual(json.loads(todo('{"action": "list"}', self.store))["items"][0]["status"], "completed")
        with self.assertRaises(ValidationError):
            todo('{"action": "set_status", "id": 1, "status": "done"}', self.store)

    def test_delete(self):
        todo('{"action": "add", "title": "整理资料"}', self.store)
        todo('{"action": "add", "title": "写周报"}', self.store)
        self.assertEqual(
            json.loads(todo('{"action": "delete", "id": 1}', self.store)),
            {"deleted": {"id": 1, "title": "整理资料", "status": "pending"}},
        )
        self.assertEqual([item["id"] for item in json.loads(todo('{"action": "list"}', self.store))["items"]], [2])

    def test_missing_id_fails(self):
        with self.assertRaises(ValueError):
            todo('{"action": "set_status", "id": 7, "status": "completed"}', self.store)
        with self.assertRaises(ValueError):
            todo('{"action": "delete", "id": 7}', self.store)

    def test_title_validation(self):
        for title in ("", 201 * "字"):
            with self.subTest(title=title), self.assertRaises(ValidationError):
                todo(json.dumps({"action": "add", "title": title}), self.store)
        with self.subTest(title="   "), self.assertRaises(ValueError):
            todo('{"action": "add", "title": "   "}', self.store)
        with self.assertRaises(ValueError):
            todo('{"action": "add"}', self.store)
        self.assertEqual(
            json.loads(todo(json.dumps({"action": "add", "title": "  整理资料  "}), self.store))["item"]["title"],
            "整理资料",
        )
        self.assertEqual(
            json.loads(todo(json.dumps({"action": "add", "title": 200 * "字"}), self.store))["item"]["title"],
            200 * "字",
        )

    def test_instances_are_isolated(self):
        other = TodoList()
        todo('{"action": "add", "title": "整理资料"}', self.store)
        todo('{"action": "add", "title": "写周报"}', other)
        self.assertEqual([item["id"] for item in json.loads(todo('{"action": "list"}', self.store))["items"]], [1])
        self.assertEqual([item["id"] for item in json.loads(todo('{"action": "list"}', other))["items"]], [1])

    def test_store_is_required(self):
        with self.assertRaises(TypeError):
            todo('{"action": "list"}')

    def test_concurrent_add(self):
        barrier = Barrier(20)

        def worker() -> None:
            barrier.wait()
            todo('{"action": "add", "title": "并发任务"}', self.store)

        threads = [Thread(target=worker) for _ in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        ids = sorted(item["id"] for item in json.loads(todo('{"action": "list"}', self.store))["items"])
        self.assertEqual(ids, list(range(1, 21)))


if __name__ == "__main__":
    unittest.main()
