from fastapi.testclient import TestClient

from src import api
from src.agent.prompt import SYSTEM_PROMPT


def test_api_origin_and_config_protection(tmp_path, monkeypatch):
    monkeypatch.setattr(api, 'CONFIG', tmp_path / 'config.json')
    monkeypatch.setattr(api, 'SESSIONS', tmp_path / 'sessions')
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    monkeypatch.delenv('OPENAI_BASE_URL', raising=False)
    monkeypatch.delenv('OPENAI_MODEL', raising=False)
    with TestClient(api.app, base_url='http://localhost', client=('127.0.0.1', 1234)) as client:
        assert client.put('/api/config', json={'model': 'm', 'api_key': 'private-key'}).status_code == 403
        headers = {'origin': 'http://localhost:5143'}
        response = client.put('/api/config', headers=headers, json={'model': 'm', 'api_key': 'private-key'})
        assert response.status_code == 200
        assert 'private-key' not in response.text
        assert client.get('/api/config').json()['api_key_set'] is True
        assert client.put('/api/config', headers=headers, json={'model': 'm', 'base_url': 'https://user:pass@example.com'}).status_code == 422
        assert client.put('/api/config', headers=headers, json={'model': 'private-key'}).status_code == 422
        assert client.get('/api/sessions', headers={'origin': 'https://evil.example'}).status_code == 403
        response = client.post('/api/sessions', headers=headers)
        assert response.status_code == 200
        session_id = response.json()['id']
        records = client.get(f'/api/sessions/{session_id}').json()['records']
        assert any(record.get('message') == {'role': 'system', 'content': SYSTEM_PROMPT} for record in records)
    with TestClient(api.app, base_url='http://localhost', client=('192.0.2.1', 1234)) as client:
        assert client.get('/api/config').status_code == 403
