import copy
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, Lock
from uuid import uuid4

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from openai import OpenAI
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .agent.agent_loop import agent_loop


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / ".data" / "conversations.json"
CONFIG = ROOT / ".data" / "config.json"
SYSTEM = {"role": "system", "content": "你是助手。需要计算时使用计算器工具。"}
load_dotenv(ROOT / ".env")

app = FastAPI()
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1"])
lock = Lock()
executor = ThreadPoolExecutor(max_workers=1)
active: dict[str, Event] = {}


def read_turns() -> list[dict]:
    if not DATA.exists():
        return []
    return json.loads(DATA.read_text(encoding="utf-8"))


def save_turns(turns: list[dict]) -> None:
    write_json(DATA, turns)


def write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    with temporary.open("w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary, path)


def read_config() -> dict:
    if not CONFIG.exists():
        return {}
    return json.loads(CONFIG.read_text(encoding="utf-8"))


def save_config(config: dict) -> None:
    write_json(CONFIG, config)


def resolve_config() -> dict[str, str]:
    config = read_config()
    return {
        "model": config.get("model") or os.environ.get("OPENAI_MODEL", ""),
        "base_url": config.get("base_url") or os.environ.get("OPENAI_BASE_URL", ""),
        "api_key": config.get("api_key") or os.environ.get("OPENAI_API_KEY", ""),
    }


def mask_key(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return "•" * len(key)
    return f"{key[:4]}{'•' * 6}{key[-4:]}"


def public_config() -> dict:
    current = resolve_config()
    return {
        "model": current["model"],
        "base_url": current["base_url"],
        "api_key": mask_key(current["api_key"]),
        "api_key_set": bool(current["api_key"]),
    }


@app.middleware("http")
async def check_origin(request: Request, call_next):
    if request.client is None or request.client.host not in {"127.0.0.1", "::1"}:
        return JSONResponse({"detail": "仅允许本机访问"}, status_code=403)
    origin = request.headers.get("origin")
    if request.method != "GET" and origin not in {"http://localhost:3000", "http://127.0.0.1:3000"}:
        return JSONResponse({"detail": "来源不允许"}, status_code=403)
    if origin and origin not in {"http://localhost:3000", "http://127.0.0.1:3000"}:
        return JSONResponse({"detail": "来源不允许"}, status_code=403)
    return await call_next(request)


@app.on_event("startup")
def recover_interrupted() -> None:
    with lock:
        turns = read_turns()
        changed = False
        for turn in turns:
            if turn["status"] in {"running", "stopping"}:
                turn["status"] = "interrupted"
                changed = True
        if changed:
            save_turns(turns)


class TurnInput(BaseModel):
    content: str = Field(min_length=1, max_length=32000)


class ConfigInput(BaseModel):
    model: str = Field(min_length=1, max_length=200)
    base_url: str = Field(default="", max_length=2000)
    api_key: str = Field(default="", max_length=2000)


def start_turn(content: str) -> dict:
    content = content.strip()
    if not content:
        raise HTTPException(status_code=422, detail="消息不能为空")
    with lock:
        if active:
            raise HTTPException(status_code=409, detail="已有任务正在执行")
        turns = read_turns()
        messages = [SYSTEM.copy()]
        for turn in turns:
            if turn["status"] == "completed":
                messages.extend(copy.deepcopy(turn["messages"]))
        messages.append({"role": "user", "content": content})
        turn = {"id": uuid4().hex, "status": "running", "messages": [messages[-1]]}
        turns.append(turn)
        save_turns(turns)
        stop = Event()
        active[turn["id"]] = stop

    def update() -> None:
        with lock:
            current = read_turns()
            item = next(item for item in current if item["id"] == turn["id"])
            item["messages"] = copy.deepcopy(messages[start_index:])
            save_turns(current)

    start_index = len(messages) - 1

    def run() -> str | None:
        settings = resolve_config()
        if not settings["model"]:
            raise RuntimeError("未配置模型")
        with OpenAI(api_key=settings["api_key"] or None, base_url=settings["base_url"] or None, max_retries=0, timeout=60.0) as client:
            return agent_loop(client, settings["model"], messages, max_turns=64, stop=stop, on_update=update)

    def finished(future) -> None:
        error = future.exception()
        with lock:
            current = read_turns()
            item = next(item for item in current if item["id"] == turn["id"])
            item["messages"] = copy.deepcopy(messages[start_index:])
            if error is not None:
                item["status"] = "failed"
                item["error"] = type(error).__name__
            elif stop.is_set():
                item["status"] = "stopped"
            elif future.result() is None:
                item["status"] = "limited"
            else:
                item["status"] = "completed"
            save_turns(current)
            del active[turn["id"]]

    future = executor.submit(run)
    future.add_done_callback(finished)
    return turn


@app.get("/api/config")
def get_config() -> dict:
    with lock:
        return public_config()


@app.put("/api/config")
def put_config(data: ConfigInput) -> dict:
    model = data.model.strip()
    base_url = data.base_url.strip()
    api_key = data.api_key.strip()
    if not model:
        raise HTTPException(status_code=422, detail="模型不能为空")
    if base_url and not base_url.startswith(("http://", "https://")):
        raise HTTPException(status_code=422, detail="接口地址必须以 http:// 或 https:// 开头")
    with lock:
        if active:
            raise HTTPException(status_code=409, detail="执行中不能修改配置")
        config = read_config()
        config["model"] = model
        config["base_url"] = base_url
        if api_key:
            config["api_key"] = api_key
        save_config(config)
        return public_config()


@app.get("/api/turns")
def get_turns() -> list[dict]:
    with lock:
        return read_turns()


@app.post("/api/turns")
def create_turn(data: TurnInput) -> dict:
    return start_turn(data.content)


@app.post("/api/turns/{turn_id}/retry")
def retry_turn(turn_id: str) -> dict:
    with lock:
        turn = next((item for item in read_turns() if item["id"] == turn_id), None)
        if turn is None:
            raise HTTPException(status_code=404, detail="消息不存在")
        if turn["status"] not in {"failed", "stopped", "limited", "interrupted"}:
            raise HTTPException(status_code=409, detail="该消息无法重试")
        content = turn["messages"][0]["content"]
    return start_turn(content)


@app.post("/api/turns/{turn_id}/stop")
def stop_turn(turn_id: str) -> dict:
    with lock:
        if turn_id not in active:
            raise HTTPException(status_code=409, detail="任务未运行")
        active[turn_id].set()
        turns = read_turns()
        turn = next(item for item in turns if item["id"] == turn_id)
        turn["status"] = "stopping"
        save_turns(turns)
        return turn
