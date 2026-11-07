import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from pydantic import ValidationError

from src.agent.tools.todo import TodoCommand, TodoList, execute, snapshot


class TodoTests(unittest.TestCase):
    def setUp(self):
        self.store = TodoList()

    def test_add_assigns_increasing_ids(self):
        first = json.loads(execute(self.store, TodoCommand(action="add", title="整理资料")))["item"]
        second = json.loads(execute(self.store, TodoCommand(action="add", title="写周报")))["item"]
        self.assertEqual(first, {"id": 1, "title": "整理资料", "status": "pending"})
        self.assertEqual(second, {"id": 2, "title": "写周报", "status": "pending"})
        self.assertEqual([item["id"] for item in json.loads(execute(self.store, TodoCommand(action="list")))["items"]], [1, 2])

    def test_list_returns_snapshot(self):
        execute(self.store, TodoCommand(action="add", title="整理资料"))
        items = snapshot(self.store)
        items[0]["title"] = "篡改"
        items.append({"id": 99})
        self.assertEqual(
            json.loads(execute(self.store, TodoCommand(action="list")))["items"],
            [{"id": 1, "title": "整理资料", "status": "pending"}],
        )

    def test_set_status_is_repeatable(self):
        execute(self.store, TodoCommand(action="add", title="整理资料"))
        command = TodoCommand(action="set_status", id=1, status="completed")
        first = json.loads(execute(self.store, command))
        second = json.loads(execute(self.store, command))
        self.assertEqual(first, {"item": {"id": 1, "title": "整理资料", "status": "completed"}})
        self.assertEqual(first, second)
        self.assertEqual(json.loads(execute(self.store, TodoCommand(action="list")))["items"][0]["status"], "completed")
        with self.assertRaises(ValidationError):
            TodoCommand(action="set_status", id=1, status="done")

    def test_delete(self):
        execute(self.store, TodoCommand(action="add", title="整理资料"))
        execute(self.store, TodoCommand(action="add", title="写周报"))
        self.assertEqual(
            json.loads(execute(self.store, TodoCommand(action="delete", id=1))),
            {"deleted": {"id": 1, "title": "整理资料", "status": "pending"}},
        )
        self.assertEqual([item["id"] for item in json.loads(execute(self.store, TodoCommand(action="list")))["items"]], [2])
        self.assertEqual(json.loads(execute(self.store, TodoCommand(action="add", title="新任务")))["item"]["id"], 3)

    def test_missing_id_fails(self):
        for command in (TodoCommand(action="set_status", id=7, status="completed"), TodoCommand(action="delete", id=7),
                        TodoCommand(action="set_status", id=1), TodoCommand(action="delete")):
            with self.subTest(command=command), self.assertRaises(ValueError):
                execute(self.store, command)

    def test_title_validation(self):
        for title in ("", 201 * "字"):
            with self.subTest(title=title), self.assertRaises(ValidationError):
                TodoCommand(action="add", title=title)
        with self.assertRaises(ValueError):
            execute(self.store, TodoCommand(action="add", title="   "))
        with self.assertRaises(ValueError):
            execute(self.store, TodoCommand(action="add"))
        self.assertEqual(json.loads(execute(self.store, TodoCommand(action="add", title="  整理资料  ")))["item"]["title"], "整理资料")
        self.assertEqual(json.loads(execute(self.store, TodoCommand(action="add", title=200 * "字")))["item"]["title"], 200 * "字")

    def test_instances_are_isolated(self):
        other = TodoList()
        execute(self.store, TodoCommand(action="add", title="整理资料"))
        execute(other, TodoCommand(action="add", title="写周报"))
        self.assertEqual(snapshot(self.store), [{"id": 1, "title": "整理资料", "status": "pending"}])
        self.assertEqual(snapshot(other), [{"id": 1, "title": "写周报", "status": "pending"}])

    def test_concurrent_add(self):
        barrier = Barrier(20)

        def worker():
            barrier.wait()
            return json.loads(execute(self.store, TodoCommand(action="add", title="并发任务")))["item"]["id"]

        with ThreadPoolExecutor(max_workers=20) as pool:
            ids = list(pool.map(lambda _: worker(), range(20)))
        self.assertEqual(sorted(ids), list(range(1, 21)))
        self.assertEqual(sorted(item["id"] for item in snapshot(self.store)), list(range(1, 21)))


if __name__ == "__main__":
    unittest.main()
