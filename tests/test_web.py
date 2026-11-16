import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('DASHBOARD_PASSWORD','this-is-a-long-test-password')
os.environ.setdefault('SESSION_SECRET','this-is-a-long-test-session-secret')
os.environ['COOKIE_SECURE']='false'

import app as web
from fastapi.testclient import TestClient


async def fake_scheduler(app):
    await app.state.stop.wait()


def test_login_dashboard_and_csrf(tmp_path, monkeypatch):
    monkeypatch.setattr(web,'DATA_DIR',tmp_path)
    monkeypatch.setattr(web,'scheduler',fake_scheduler)
    monkeypatch.setattr(web,'COOKIE_SECURE',False)
    with TestClient(web.app) as client:
        assert client.get('/', follow_redirects=False).status_code in (302,303,307)
        assert client.get('/api/state').status_code==401
        assert client.post('/api/login',json={'password':'wrong'}).status_code==401
        response=client.post('/api/login',json={'password':os.environ['DASHBOARD_PASSWORD']})
        assert response.status_code==200
        assert client.get('/').status_code==200
        data=client.get('/api/state').json()
        assert data['stats']['known']==0
        assert data['telegram_ready'] is False
        assert client.post('/api/telegram/test').status_code==403
        assert client.post('/api/telegram/test',headers={'X-CSRF-Token':data['csrf']}).status_code==400
        assert client.post('/api/logout',headers={'X-CSRF-Token':data['csrf']}).status_code==200
        assert client.get('/api/state').status_code==401


def test_find_and_verify_telegram_recipient(tmp_path, monkeypatch):
    monkeypatch.setattr(web, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(web, 'scheduler', fake_scheduler)
    monkeypatch.setattr(web, 'COOKIE_SECURE', False)
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', 'test-only-do-not-send')
    monkeypatch.delenv('TELEGRAM_CHAT_ID', raising=False)
    monkeypatch.setattr(web, 'telegram_recent_chats', lambda: [{'id':'123456789','name':'Test recipient'}])
    messages = []

    def fake_send(message, *, chat_id=None, required=True):
        messages.append((message, chat_id))

    monkeypatch.setattr(web, 'telegram_send', fake_send)
    with TestClient(web.app) as client:
        state = client.get('/api/state')
        assert state.status_code == 401
        client.post('/api/login', json={'password': os.environ['DASHBOARD_PASSWORD']})
        state = client.get('/api/state').json()
        assert state['telegram_bot_ready'] is True
        assert state['telegram_ready'] is False
        assert 'TELEGRAM_BOT_TOKEN' not in state
        csrf = {'X-CSRF-Token': state['csrf']}
        assert client.post('/api/telegram/chats').status_code == 403
        found = client.post('/api/telegram/chats', headers=csrf)
        assert found.status_code == 200
        assert found.json()['chats'] == [{'id':'123456789','name':'Test recipient'}]
        wrong = client.post('/api/telegram/recipient', json={'chat_id':'not-a-chat'}, headers=csrf)
        assert wrong.status_code == 422
        response = client.post('/api/telegram/recipient', json={'chat_id':'123456789'}, headers=csrf)
        assert response.status_code == 200
        assert messages and messages[0][1] == '123456789'
        state = client.get('/api/state').json()
        assert state['telegram_chat_id'] == '123456789'
        assert state['telegram_ready'] is True
        assert client.post('/api/telegram/test', headers=csrf).status_code == 200
        assert messages[-1][1] == '123456789'
        assert 'TELEGRAM_BOT_TOKEN' not in client.get('/api/state').text


def test_failed_telegram_verification_not_saved(tmp_path, monkeypatch):
    monkeypatch.setattr(web, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(web, 'scheduler', fake_scheduler)
    monkeypatch.setattr(web, 'COOKIE_SECURE', False)
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', 'test-only-do-not-send')
    monkeypatch.delenv('TELEGRAM_CHAT_ID', raising=False)

    def fail_send(*args, **kwargs):
        raise RuntimeError('Telegram could not deliver the message: chat not found')

    monkeypatch.setattr(web, 'telegram_send', fail_send)
    with TestClient(web.app) as client:
        client.post('/api/login', json={'password': os.environ['DASHBOARD_PASSWORD']})
        csrf = {'X-CSRF-Token': client.get('/api/state').json()['csrf']}
        response = client.post('/api/telegram/recipient', json={'chat_id': '8855842471'}, headers=csrf)
        assert response.status_code == 502
        data = client.get('/api/state').json()
        assert data['telegram_chat_id'] == ''
        assert data['telegram_ready'] is False
