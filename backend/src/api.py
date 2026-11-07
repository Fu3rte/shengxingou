import asyncio
import json
import os
import re
from contextlib import ExitStack, asynccontextmanager
from pathlib import Path
from threading import RLock
from urllib.parse import urlsplit

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from openai import AsyncOpenAI
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .agent.agent_loop import AgentLoop, close_model_client
from .agent.session import SessionManager
from .agent.types import SystemMessage

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / '.data' / 'config.json'
SESSIONS = ROOT / '.data' / 'sessions'
load_dotenv(ROOT / '.env')
lock = RLock()
loops: dict[str, tuple[AgentLoop, AsyncOpenAI]] = {}
TERMINAL = {'run.completed', 'run.finished'}


def read_config():
    return json.loads(CONFIG.read_text(encoding='utf-8')) if CONFIG.exists() else {}


def resolve_config():
    saved = read_config()
    return {key: saved.get(key) or os.environ.get(env, '') for key, env in (
        ('model', 'OPENAI_MODEL'), ('base_url', 'OPENAI_BASE_URL'), ('api_key', 'OPENAI_API_KEY'))}


def public_config():
    settings = resolve_config()
    return {'model': settings['model'], 'base_url': settings['base_url'],
            'api_key': '••••••••' if settings['api_key'] else '', 'api_key_set': bool(settings['api_key'])}


def manager():
    settings = resolve_config()
    saved = read_config()
    return SessionManager(SESSIONS, secrets=tuple(filter(None, (settings['api_key'], saved.get('api_key'), os.environ.get('OPENAI_API_KEY')))))


def session_or_404(session_id):
    sessions = manager()
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', session_id) or not (sessions.root / f'{session_id}.jsonl').exists():
        raise HTTPException(404, '会话不存在或无效')
    return sessions.open(session_id)


def get_loop(session_id):
    session = session_or_404(session_id)
    if session_id not in loops:
        settings = resolve_config()
        if not settings['model'] or not settings['api_key']:
            raise HTTPException(422, '请配置模型和 API Key')
        client = AsyncOpenAI(api_key=settings['api_key'], base_url=settings['base_url'] or None, max_retries=0, timeout=60.0)
        with ExitStack() as cleanup:
            cleanup.callback(close_model_client, client)
            loop = AgentLoop(session, client, settings['model'])
            loops[session_id] = (loop, client)
            cleanup.pop_all()
    return loops[session_id][0]


def close_loops():
    with ExitStack() as cleanup:
        for loop, client in loops.values():
            cleanup.callback(close_model_client, client)
            cleanup.callback(loop.close)
        loops.clear()


@asynccontextmanager
async def lifespan(app):
    await asyncio.to_thread(manager().recover)
    try:
        yield
    finally:
        def shutdown():
            with lock:
                close_loops()
        await asyncio.to_thread(shutdown)


app = FastAPI(lifespan=lifespan)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=['localhost', '127.0.0.1'])


@app.middleware('http')
async def check_origin(request: Request, call_next):
    if request.client is None or request.client.host not in {'127.0.0.1', '::1'}:
        return JSONResponse({'detail': '仅允许本机访问'}, status_code=403)
    origin = request.headers.get('origin')
    allowed = {'http://localhost:3000', 'http://127.0.0.1:3000'}
    if (origin and origin not in allowed) or (request.method not in {'GET', 'HEAD'} and origin not in allowed):
        return JSONResponse({'detail': '来源不允许'}, status_code=403)
    return await call_next(request)


class ConfigInput(BaseModel):
    model: str = Field(min_length=1, max_length=200)
    base_url: str = Field(default='', max_length=2000)
    api_key: str = Field(default='', max_length=2000)


class MessageInput(BaseModel):
    content: str = Field(min_length=1, max_length=32000)


class ConfirmationInput(BaseModel):
    turn_index: int = Field(ge=1)
    tool_call_id: str = Field(min_length=1)
    decision: str


@app.get('/api/config')
def get_config():
    with lock:
        return public_config()


@app.put('/api/config')
def put_config(data: ConfigInput):
    model, base_url, api_key = data.model.strip(), data.base_url.strip(), data.api_key.strip()
    url = urlsplit(base_url)
    if not model or (base_url and (url.scheme not in {'http', 'https'} or not url.hostname or url.username or url.password or url.query or url.fragment)):
        raise HTTPException(422, '模型或接口地址无效')
    with lock:
        if any(loop.state.active is not None for loop, _ in loops.values()):
            raise HTTPException(409, '执行中不能修改配置')
        current = read_config()
        key = api_key or current.get('api_key') or os.environ.get('OPENAI_API_KEY', '')
        env_key = os.environ.get('OPENAI_API_KEY', '')
        if any(secret and (secret in model or secret in base_url) for secret in (key, env_key)):
            raise HTTPException(422, '模型或接口地址包含密钥')
        close_loops()
        CONFIG.parent.mkdir(parents=True, exist_ok=True)
        temp = CONFIG.with_suffix('.json.tmp')
        with temp.open('w', encoding='utf-8') as file:
            json.dump({'model': model, 'base_url': base_url, 'api_key': api_key or current.get('api_key', '')}, file, ensure_ascii=False)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp, CONFIG)
        return public_config()


@app.get('/api/sessions')
def list_sessions():
    with lock:
        return [{'id': id, 'title': title} for id, title in manager().list()]


@app.post('/api/sessions')
def create_session():
    with lock:
        session = manager().create()
        session.append_message(SystemMessage(content='你是助手。需要计算时使用计算器工具。'), None)
        return {'id': session.id}


@app.get('/api/sessions/{session_id}')
def get_session(session_id: str):
    with lock:
        session = session_or_404(session_id)
        return {'id': session.id, 'records': [record.model_dump(mode='json') for record in session.read()]}


@app.delete('/api/sessions/{session_id}')
def delete_session(session_id: str):
    with lock:
        session_or_404(session_id).path.unlink()
    return {'ok': True}


@app.post('/api/sessions/{session_id}/messages')
def append_message(session_id: str, data: MessageInput):
    with lock:
        loop = get_loop(session_id)
        if not data.content.strip() or loop.session.guard.contains(data.content):
            raise HTTPException(422, '消息无效或包含密钥')
        return {'run_id': loop.append_message(data.content)}


@app.get('/api/sessions/{session_id}/runs/{run_id}/events')
def trace(session_id: str, run_id: str):
    with lock:
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', run_id):
            raise HTTPException(404, '运行不存在')
        events = session_or_404(session_id).trace(run_id)
        if not events:
            raise HTTPException(404, '运行不存在')
        return [event.model_dump(mode='json') for event in events]


@app.get('/api/sessions/{session_id}/runs/{run_id}/stream')
async def stream(session_id: str, run_id: str, request: Request, after: str = '0'):
    cursor = request.headers.get('last-event-id', after)
    if len(cursor) > 20 or not cursor.isascii() or not cursor.isdecimal():
        raise HTTPException(422, '无效事件游标')
    cursor = int(cursor)
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', run_id):
        raise HTTPException(404, '运行不存在')
    session = await asyncio.to_thread(session_or_404, session_id)
    size = session.path.stat().st_size
    events = await asyncio.to_thread(session.trace, run_id)
    if not events:
        raise HTTPException(404, '运行不存在')
    if cursor > events[-1].seq:
        raise HTTPException(422, '无效事件游标')

    with lock:
        loop = loops.get(session_id, (None, None))[0]
    if events[-1].type not in TERMINAL and loop is None:
        raise RuntimeError('运行不在本进程中')

    def next_events(previous_size):
        with loop.state.condition:
            changed = loop.state.condition.wait_for(
                lambda: session.path.stat().st_size != previous_size or run_id in loop.state.finished,
                timeout=1,
            )
            if not changed:
                return None
            next_size = session.path.stat().st_size
            next_snapshot = session.trace(run_id)
            if run_id in loop.state.finished and next_snapshot[-1].type not in TERMINAL:
                raise RuntimeError('运行终态未持久化')
            return next_size, next_snapshot

    async def generate():
        nonlocal cursor, events, size
        while True:
            for event in events:
                if event.seq > cursor:
                    cursor = event.seq
                    yield f'id: {event.seq}\ndata: {event.model_dump_json()}\n\n'
            if events[-1].type in TERMINAL:
                return
            update = await asyncio.to_thread(next_events, size)
            if update is not None:
                size, events = update

    return StreamingResponse(generate(), media_type='text/event-stream', headers={'Cache-Control': 'no-cache, no-transform', 'X-Accel-Buffering': 'no'})


@app.post('/api/sessions/{session_id}/runs/{run_id}/stop')
def stop(session_id: str, run_id: str):
    with lock:
        session_or_404(session_id)
        loop = loops.get(session_id, (None, None))[0]
        if loop is None:
            raise HTTPException(409, '运行不在执行中')
        with loop.state.condition:
            if loop.state.active != run_id:
                raise HTTPException(409, '运行不在执行中')
            if not loop.abort(run_id):
                raise HTTPException(409, '运行已结束')
    return {'ok': True}


@app.post('/api/sessions/{session_id}/runs/{run_id}/confirm')
def confirm(session_id: str, run_id: str, data: ConfirmationInput):
    with lock:
        session_or_404(session_id)
        loop = loops.get(session_id, (None, None))[0]
        if loop is None:
            raise HTTPException(409, '确认请求无效')
        with loop.state.condition:
            if (data.decision not in {'approved', 'rejected'} or loop.state.active != run_id
                    or loop.state.stop or loop.state.shutdown or loop.state.decision is not None
                    or loop.state.confirmation != (run_id, data.turn_index, data.tool_call_id)):
                raise HTTPException(409, '确认请求无效')
            loop.confirm(run_id, data.turn_index, data.tool_call_id, data.decision)
        return {'ok': True}
