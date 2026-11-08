import base64
import os
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ReadFile(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    path: str = Field(min_length=1)
    encoding: Literal["utf-8", "base64"] = "utf-8"


class WriteFile(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    path: str = Field(min_length=1)
    content: str
    mode: Literal["append", "overwrite"]
    encoding: Literal["utf-8", "base64"] = "utf-8"


def read_bytes(path: Path) -> bytes:
    return path.read_bytes()


def write_bytes(path: Path, data: bytes, mode: Literal["append", "overwrite", "create"]) -> int:
    with path.open({"append": "ab", "overwrite": "wb", "create": "xb"}[mode]) as file:
        count = file.write(data)
        file.flush()
        os.fsync(file.fileno())
        return count


def decode_content(content: str, encoding: Literal["utf-8", "base64"]) -> bytes:
    if encoding == "base64":
        return base64.b64decode(content, validate=True)
    return content.encode("utf-8")


def read(parameters: ReadFile, *, start: Callable[[], bool] | None = None) -> str | None:
    if start is not None and not start():
        return None
    data = read_bytes(Path(parameters.path))
    if parameters.encoding == "base64":
        return base64.b64encode(data).decode("ascii")
    return data.decode("utf-8")


def write(parameters: WriteFile, *, start: Callable[[], bool] | None = None) -> str | None:
    if start is not None and not start():
        return None
    data = decode_content(parameters.content, parameters.encoding)
    count = write_bytes(Path(parameters.path), data, parameters.mode)
    return str(count)
