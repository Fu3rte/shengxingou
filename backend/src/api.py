import asyncio
import json
import os
import re
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from openai import AsyncOpenAI
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .agent.runtime import (
    Agent,
    InvalidConfirmation,
    abort,
    append_message,
    close,
    confirm,
    create_agent,
    events,
    is_active,
    protect,
)
from .agent.security import contains
from .agent import session as sessions
from .agent.session import (
    create_session as create_session_file,
    list_sessions as list_session_files,
    open_session,
    recover_sessions,
    run_sync,
)
from .agent.types import SystemMessage

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / ".data" / "config.json"
SESSIONS = ROOT / ".data" / "sessions"
load_dotenv(ROOT / ".env")
lock = asyncio.Lock()
loops: dict[str, tuple[Agent, AsyncOpenAI]] = {}
TERMINAL = {"run.completed", "run.finished"}


class ConfigInput(BaseModel):
    model: str = Field(min_length=1, max_length=200)
    base_url: str = Field(default="", max_length=2000)
    api_key: str = Field(default="", max_length=2000)


class MessageInput(BaseModel):
    content: str = Field(min_length=1, max_length=32000)


class ConfirmationInput(BaseModel):
    turn_index: int = Field(ge=1)
    tool_call_id: str = Field(min_length=1)
    decision: str


def read_config():
    return json.loads(CONFIG.read_text(encoding="utf-8")) if CONFIG.exists() else {}


def resolve_config():
    saved = read_config()
    return {
        key: saved.get(key) or os.environ.get(env, "")
        for key, env in (
            ("model", "OPENAI_MODEL"),
            ("base_url", "OPENAI_BASE_URL"),
            ("api_key", "OPENAI_API_KEY"),
        )
    }


def public_config():
    settings = resolve_config()
    return {
        "model": settings["model"],
        "base_url": settings["base_url"],
        "api_key": "••••••••" if settings["api_key"] else "",
        "api_key_set": bool(settings["api_key"]),
    }


def session_secrets():
    settings = resolve_config()
    saved = read_config()
    return tuple(
        filter(
            None,
            (
                settings["api_key"],
                saved.get("api_key"),
                os.environ.get("OPENAI_API_KEY"),
            ),
        )
    )


def session_or_404(session_id):
    if (
        not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", session_id)
        or not (SESSIONS.resolve() / f"{session_id}.jsonl").exists()
    ):
        raise HTTPException(404, "会话不存在或无效")
    return open_session(SESSIONS, session_id, secrets=session_secrets())


async def _close_client(client):
    await protect(client.close())


async def get_loop(session_id):
    return await protect(_get_loop(session_id))


async def _get_loop(session_id):
    session = await run_sync(session_or_404, session_id)
    if session_id not in loops:
        settings = await run_sync(resolve_config)
        if not settings["model"] or not settings["api_key"]:
            raise HTTPException(422, "请配置模型和 API Key")
        client = await run_sync(
            AsyncOpenAI,
            api_key=settings["api_key"],
            base_url=settings["base_url"] or None,
            max_retries=0,
            timeout=60.0,
        )
        async with AsyncExitStack() as cleanup:
            cleanup.push_async_callback(_close_client, client)
            agent = await create_agent(session, client, settings["model"])
            loops[session_id] = (agent, client)
            cleanup.pop_all()
    return loops[session_id][0]


async def close_loops():
    async with AsyncExitStack() as cleanup:
        for agent, client in loops.values():
            cleanup.push_async_callback(_close_client, client)
            cleanup.push_async_callback(close, agent)
        loops.clear()


@asynccontextmanager
async def lifespan(app):
    await run_sync(lambda: recover_sessions(SESSIONS, secrets=session_secrets()))
    try:
        yield
    finally:
        async with lock:
            await close_loops()


app = FastAPI(lifespan=lifespan)
allowed_origins = {"http://localhost:5143", "http://127.0.0.1:5143"}
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1"])
app.add_middleware(CORSMiddleware, allow_origins=list(allowed_origins), allow_methods=["*"], allow_headers=["*"])


@app.exception_handler(InvalidConfirmation)
async def invalid_confirmation(request: Request, error: InvalidConfirmation):
    return JSONResponse({"detail": "确认请求无效"}, status_code=409)


@app.middleware("http")
async def check_origin(request: Request, call_next):
    if request.client is None or request.client.host not in {"127.0.0.1", "::1"}:
        return JSONResponse({"detail": "仅允许本机访问"}, status_code=403)
    origin = request.headers.get("origin")
    if (origin and origin not in allowed_origins) or (
        request.method not in {"GET", "HEAD"} and origin not in allowed_origins
    ):
        return JSONResponse({"detail": "来源不允许"}, status_code=403)
    return await call_next(request)

@app.get("/api/config")
async def get_config():
    async with lock:
        return await run_sync(public_config)


def _write_config(model, base_url, api_key):
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    temp = CONFIG.with_suffix(".json.tmp")
    with temp.open("w", encoding="utf-8") as file:
        json.dump(
            {"model": model, "base_url": base_url, "api_key": api_key},
            file,
            ensure_ascii=False,
        )
        file.flush()
        os.fsync(file.fileno())
    os.replace(temp, CONFIG)
    return public_config()


@app.put("/api/config")
async def put_config(data: ConfigInput):
    model, base_url, api_key = (
        data.model.strip(),
        data.base_url.strip(),
        data.api_key.strip(),
    )
    url = urlsplit(base_url)
    if not model or (
        base_url
        and (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
        )
    ):
        raise HTTPException(422, "模型或接口地址无效")
    async with lock:
        if any(is_active(agent) for agent, _ in loops.values()):
            raise HTTPException(409, "执行中不能修改配置")
        current = await run_sync(read_config)
        key = api_key or current.get("api_key") or os.environ.get("OPENAI_API_KEY", "")
        env_key = os.environ.get("OPENAI_API_KEY", "")
        if any(
            secret and (secret in model or secret in base_url)
            for secret in (key, env_key)
        ):
            raise HTTPException(422, "模型配置包含密钥")
        await close_loops()
        return await run_sync(
            _write_config, model, base_url, api_key or current.get("api_key", "")
        )


@app.get("/api/sessions")
async def list_sessions():
    async with lock:
        return await run_sync(
            lambda: [
                {"id": id, "title": title}
                for id, title in list_session_files(SESSIONS, secrets=session_secrets())
            ]
        )


def _create_session():
    session = create_session_file(SESSIONS, secrets=session_secrets())
    sessions.append_message(
        session,
        SystemMessage(content="你是助手。需要计算时使用计算器工具。"), None
    )
    return {"id": session.id}


@app.post("/api/sessions")
async def create_session():
    async with lock:
        return await run_sync(_create_session)


def _get_session(session_id):
    session = session_or_404(session_id)
    return {
        "id": session.id,
        "records": [record.model_dump(mode="json") for record in sessions.read(session)],
    }


@app.get("/api/sessions/{session_id}")
async def get_session(session_id: str):
    async with lock:
        return await run_sync(_get_session, session_id)


def _delete_session(session_id):
    session_or_404(session_id).path.unlink()


@app.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str):
    async with lock:
        await run_sync(_delete_session, session_id)
    return {"ok": True}


@app.post("/api/sessions/{session_id}/messages")
async def post_message(session_id: str, data: MessageInput):
    async with lock:
        agent = await get_loop(session_id)
        if not data.content.strip() or contains(agent.session.guard, data.content):
            raise HTTPException(422, "消息无效或包含密钥")
        return {"run_id": await append_message(agent, data.content)}


def _trace(session_id, run_id):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", run_id):
        raise HTTPException(404, "运行不存在")
    snapshot = sessions.trace(session_or_404(session_id), run_id)
    if not snapshot:
        raise HTTPException(404, "运行不存在")
    return [event.model_dump(mode="json") for event in snapshot]


@app.get("/api/sessions/{session_id}/runs/{run_id}/events")
async def trace_events(session_id: str, run_id: str):
    async with lock:
        return await run_sync(_trace, session_id, run_id)


@app.get("/api/sessions/{session_id}/runs/{run_id}/stream")
async def stream(session_id: str, run_id: str, request: Request, after: str = "0"):
    cursor = request.headers.get("last-event-id", after)
    if len(cursor) > 20 or not cursor.isascii() or not cursor.isdecimal():
        raise HTTPException(422, "无效事件游标")
    cursor = int(cursor)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", run_id):
        raise HTTPException(404, "运行不存在")
    session = await run_sync(session_or_404, session_id)
    snapshot = await run_sync(sessions.trace, session, run_id)
    if not snapshot:
        raise HTTPException(404, "运行不存在")
    if cursor > snapshot[-1].seq:
        raise HTTPException(422, "无效事件游标")
    async with lock:
        agent = loops.get(session_id, (None, None))[0]
    if snapshot[-1].type not in TERMINAL and agent is None:
        raise RuntimeError("运行不在本进程中")

    async def generate():
        if snapshot[-1].type in TERMINAL:
            for event in snapshot:
                if event.seq > cursor:
                    yield f"id: {event.seq}\ndata: {event.model_dump_json()}\n\n"
            return
        async for event in events(agent, run_id, cursor):
            yield f"id: {event.seq}\ndata: {event.model_dump_json()}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


@app.post("/api/sessions/{session_id}/runs/{run_id}/stop")
async def stop(session_id: str, run_id: str):
    async with lock:
        await run_sync(session_or_404, session_id)
        agent = loops.get(session_id, (None, None))[0]
        if agent is None or not is_active(agent, run_id):
            raise HTTPException(409, "运行不在执行中")
        if not await abort(agent, run_id):
            raise HTTPException(409, "运行已结束")
    return {"ok": True}


@app.post("/api/sessions/{session_id}/runs/{run_id}/confirm")
async def confirm_tool(session_id: str, run_id: str, data: ConfirmationInput):
    async with lock:
        await run_sync(session_or_404, session_id)
        agent = loops.get(session_id, (None, None))[0]
        if agent is None:
            raise HTTPException(409, "确认请求无效")
        await confirm(agent, run_id, data.turn_index, data.tool_call_id, data.decision)
        return {"ok": True}
