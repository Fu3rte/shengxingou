import base64
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel


@dataclass(frozen=True)
class SecretGuard:
    secrets: tuple[str, ...] = field(repr=False)


@dataclass
class StreamSecretGuard:
    guard: SecretGuard
    pending: list[dict[str, str]] = field(default_factory=list)
    finished: bool = False


def create_guard(secrets: Sequence[str]) -> SecretGuard:
    return SecretGuard(tuple(secret for secret in secrets if secret))


def contains(guard: SecretGuard, value: Any) -> bool:
    if isinstance(value, str):
        return any(secret in value for secret in guard.secrets)
    if isinstance(value, bytes):
        return any(secret.encode("utf-8") in value for secret in guard.secrets)
    if isinstance(value, BaseModel):
        return contains(guard, value.model_dump(mode="json"))
    if isinstance(value, Mapping):
        if (
            value.get("encoding") == "base64"
            and isinstance(value.get("content"), str)
            and "path" in value
            and "mode" in value
            and contains(guard, base64.b64decode(value["content"], validate=True))
        ):
            return True
        return any(
            contains(guard, key) or contains(guard, item) for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(contains(guard, item) for item in value)
    return False


def check(guard: SecretGuard, value: Any) -> None:
    if contains(guard, value):
        raise ValueError("内容包含已配置密钥")


def feed(
    state: StreamSecretGuard, fragments: Mapping[str, str]
) -> list[dict[str, str]]:
    if state.finished:
        raise ValueError("流已经结束")
    event = dict(fragments)
    if not all(
        isinstance(key, str) and isinstance(value, str) for key, value in event.items()
    ):
        raise TypeError("流片段必须为字符串")
    state.pending.append(event)
    streams: dict[str, str] = {}
    for item in state.pending:
        for key, value in item.items():
            streams[key] = streams.get(key, "") + value
    if contains(state.guard, event) or contains(state.guard, streams):
        state.finished = True
        state.pending.clear()
        raise ValueError("内容包含已配置密钥")
    earliest = len(state.pending)
    for key, text in streams.items():
        overlap = max(
            (
                length
                for secret in state.guard.secrets
                for length in range(1, min(len(text), len(secret) - 1) + 1)
                if text.endswith(secret[:length])
            ),
            default=0,
        )
        if not overlap:
            continue
        consumed = 0
        for index in range(len(state.pending) - 1, -1, -1):
            consumed += len(state.pending[index].get(key, ""))
            if consumed >= overlap:
                earliest = min(earliest, index)
                break
    safe = state.pending[:earliest]
    del state.pending[:earliest]
    return safe


def finish(state: StreamSecretGuard) -> list[dict[str, str]]:
    if state.finished:
        raise ValueError("流已经结束")
    state.finished = True
    safe = state.pending
    state.pending = []
    return safe
