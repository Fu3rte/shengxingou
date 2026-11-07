import json
import unittest

from fastapi.testclient import TestClient

from src.api import SESSIONS, app, manager


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app, base_url='http://127.0.0.1', client=('127.0.0.1', 1234))
        self.addCleanup(self.client.close)

    def test_origin_restrictions(self):
        self.assertEqual(self.client.get('/api/config', headers={'origin': 'https://evil.example'}).status_code, 403)
        self.assertEqual(self.client.post('/api/sessions').status_code, 403)
        self.assertEqual(self.client.get('/api/config', headers={'origin': 'http://127.0.0.1:3000'}).status_code, 200)

    def test_invalid_session_and_cursor(self):
        self.assertEqual(self.client.get('/api/sessions/invalid!/runs/run/events').status_code, 404)
        self.assertEqual(self.client.get('/api/sessions/invalid/runs/run/stream?after=-1').status_code, 422)
        self.assertEqual(self.client.get('/api/sessions/invalid/runs/run/stream', headers={'Last-Event-ID': 'x'}).status_code, 422)

    def test_persisted_sse_replay_and_session_isolation(self):
        sessions = manager()
        first, second = sessions.create(), sessions.create()
        for session in (first, second):
            self.addCleanup(session.path.unlink, missing_ok=True)
            for suffix in ('.lock', '.stream.lock', '.tool.lock', '.run.lock'):
                self.addCleanup((SESSIONS / '.locks' / f'{session.id}{suffix}').unlink, missing_ok=True)
        first.append_event('run1', 'run.started', {'input': 'hello'})
        first.append_event('run1', 'run.finished', {'status': 'stopped'})
        path = f'/api/sessions/{first.id}/runs/run1'
        snapshot = self.client.get(path + '/events').json()
        response = self.client.get(path + '/stream?after=0', headers={'Last-Event-ID': '1'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual([json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')], snapshot[1:])
        self.assertEqual(self.client.get(path + '/stream?after=3').status_code, 422)
        self.assertEqual(self.client.get(f'/api/sessions/{second.id}/runs/run1/events').status_code, 404)


if __name__ == '__main__':
    unittest.main()
