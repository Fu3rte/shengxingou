from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel


class SecretGuard:
    def __init__(self, secrets: Sequence[str]) -> None:
        self.secrets = tuple(secret for secret in secrets if secret)

    def contains(self, value: Any) -> bool:
        if isinstance(value, str):
            return any(secret in value for secret in self.secrets)
        if isinstance(value, BaseModel):
            return self.contains(value.model_dump(mode="json"))
        if isinstance(value, Mapping):
            return any(self.contains(key) or self.contains(item) for key, item in value.items())
        if isinstance(value, (list, tuple)):
            return any(self.contains(item) for item in value)
        return False

    def check(self, value: Any) -> None:
        if self.contains(value):
            raise ValueError("内容包含已配置密钥")


class StreamSecretGuard:
    def __init__(self, secrets: Sequence[str]) -> None:
        self.guard = SecretGuard(secrets)
        self.pending: list[dict[str, str]] = []
        self.finished = False

    def feed(self, fragments: Mapping[str, str]) -> list[dict[str, str]]:
        if self.finished:
            raise ValueError("流已经结束")
        event = dict(fragments)
        if not all(isinstance(key, str) and isinstance(value, str) for key, value in event.items()):
            raise TypeError("流片段必须为字符串")
        self.pending.append(event)
        streams: dict[str, str] = {}
        for item in self.pending:
            for key, value in item.items():
                streams[key] = streams.get(key, "") + value
        if self.guard.contains(event) or self.guard.contains(streams):
            self.finished = True
            self.pending.clear()
            raise ValueError("内容包含已配置密钥")
        earliest = len(self.pending)
        for key, text in streams.items():
            overlap = max((length for secret in self.guard.secrets
                           for length in range(1, min(len(text), len(secret) - 1) + 1)
                           if text.endswith(secret[:length])), default=0)
            if not overlap:
                continue
            consumed = 0
            for index in range(len(self.pending) - 1, -1, -1):
                consumed += len(self.pending[index].get(key, ""))
                if consumed >= overlap:
                    earliest = min(earliest, index)
                    break
        safe = self.pending[:earliest]
        del self.pending[:earliest]
        return safe

    def finish(self) -> list[dict[str, str]]:
        if self.finished:
            raise ValueError("流已经结束")
        self.finished = True
        safe = self.pending
        self.pending = []
        return safe
