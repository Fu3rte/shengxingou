import json
import unittest

from fastapi.testclient import TestClient

from src.agent.session import create_session
from src.api import SESSIONS, app, session_secrets
from tests.test_normalized_stream import chunk
from tests.test_session import record_model_stream


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app, base_url='http://127.0.0.1', client=('127.0.0.1', 1234))
        self.addCleanup(self.client.close)

    def test_origin_restrictions(self):
        for origin in ('http://127.0.0.1:5143', 'http://localhost:5143'):
            with self.subTest(origin=origin):
                headers = {'origin': origin}
                response = self.client.options('/api/sessions', headers={
                    **headers,
                    'access-control-request-method': 'POST',
                    'access-control-request-headers': 'content-type',
                })
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers['access-control-allow-origin'], origin)
                response = self.client.post('/api/sessions', headers=headers)
                self.assertEqual(response.status_code, 200)
                session_id = response.json()['id']
                self.addCleanup((SESSIONS / f'{session_id}.jsonl').unlink, missing_ok=True)
                for suffix in ('.lock', '.stream.lock', '.tool.lock', '.run.lock'):
                    self.addCleanup((SESSIONS / '.locks' / f'{session_id}{suffix}').unlink, missing_ok=True)
                self.assertEqual(self.client.get('/api/sessions/' + session_id).status_code, 200)

        for origin in ('http://localhost:3000', 'https://example.com', 'http://127.0.0.1:5144'):
            with self.subTest(origin=origin):
                headers = {'origin': origin}
                self.assertEqual(self.client.get('/api/config', headers=headers).status_code, 403)
                self.assertEqual(self.client.post('/api/sessions', headers=headers).status_code, 403)
                self.assertEqual(self.client.options('/api/sessions', headers={**headers, 'access-control-request-method': 'POST'}).status_code, 403)
        self.assertEqual(self.client.post('/api/sessions').status_code, 403)
        self.assertEqual(self.client.get('/api/config').status_code, 200)
        self.assertEqual(self.client.get('/api/config', headers={'host': 'evil.example'}).status_code, 400)
        remote = TestClient(app, base_url='http://127.0.0.1', client=('192.0.2.1', 1234))
        self.addCleanup(remote.close)
        self.assertEqual(remote.get('/api/config').status_code, 403)

    def test_invalid_session_and_cursor(self):
        self.assertEqual(self.client.get('/api/sessions/invalid!/runs/run/events').status_code, 404)
        self.assertEqual(self.client.get('/api/sessions/invalid/runs/run/stream?after=-1').status_code, 422)
        self.assertEqual(self.client.get('/api/sessions/invalid/runs/run/stream', headers={'Last-Event-ID': 'x'}).status_code, 422)

    def test_delete_session(self):
        first, second = (create_session(SESSIONS, secrets=session_secrets()) for _ in range(2))
        for session in (first, second):
            self.addCleanup(session.path.unlink, missing_ok=True)
            for suffix in ('.lock', '.stream.lock', '.tool.lock', '.run.lock'):
                self.addCleanup((SESSIONS / '.locks' / f'{session.id}{suffix}').unlink, missing_ok=True)
        path = f'/api/sessions/{first.id}'
        headers = {'origin': 'http://127.0.0.1:5143'}
        self.assertEqual(self.client.delete(path).status_code, 403)
        self.assertEqual(self.client.delete(path, headers=headers).status_code, 200)
        self.assertFalse(first.path.exists())
        self.assertEqual(self.client.get(path).status_code, 404)
        self.assertEqual(self.client.delete(path, headers=headers).status_code, 404)
        self.assertEqual(self.client.delete('/api/sessions/invalid!', headers=headers).status_code, 404)
        self.assertNotIn(first.id, [item['id'] for item in self.client.get('/api/sessions').json()])
        self.assertEqual(self.client.get(f'/api/sessions/{second.id}').status_code, 200)

    def test_reasoning_snapshot_and_sse_replay(self):
        session = create_session(SESSIONS, secrets=session_secrets())
        self.addCleanup(session.path.unlink, missing_ok=True)
        for suffix in ('.lock', '.stream.lock', '.tool.lock', '.run.lock'):
            self.addCleanup((SESSIONS / '.locks' / f'{session.id}{suffix}').unlink, missing_ok=True)
        session.append_event('run1', 'run.started', {'input': 'hello'})
        session.append_event('run1', 'model.request', {'turn_index': 1, 'model': 'example', 'messages': [], 'tools': []})
        list(record_model_stream(session, 'run1', 1, [
            chunk({'role': 'assistant', 'reasoning_content': 'thinking'}),
            chunk({'content': 'answer'}), chunk({}, 'stop'),
        ]))
        session.append_event('run1', 'run.finished', {'status': 'stopped'})
        path = f'/api/sessions/{session.id}/runs/run1'
        snapshot = self.client.get(path + '/events').json()
        reasoning = next(event for event in snapshot if event['type'] == 'model.delta' and 'reasoning' in event['data']['fragments'])
        self.assertEqual(reasoning['data']['fragments']['reasoning'], 'thinking')
        response = self.client.get(path + '/stream?after=0', headers={'Last-Event-ID': str(reasoning['seq'] - 1)})
        replay = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
        self.assertEqual(replay, snapshot[reasoning['seq'] - 1:])

    def test_persisted_sse_replay_and_session_isolation(self):
        first, second = (create_session(SESSIONS, secrets=session_secrets()) for _ in range(2))
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
