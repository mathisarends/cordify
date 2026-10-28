from dataclasses import dataclass
from time import time_ns
from typing import Annotated, Protocol

from pydantic import AfterValidator, ConfigDict, Field, StringConstraints, TypeAdapter


def _portable_key(key: str) -> str:
    devices = {"CON", "PRN", "AUX", "NUL"} | {
        f"{prefix}{index}" for prefix in ("COM", "LPT") for index in range(1, 10)
    }
    if any(part.endswith(".") or part.split(".")[0].upper() in devices for part in key.split("/")):
        raise ValueError("Session file keys must be portable relative names")
    return key


type FileKey = Annotated[
    str,
    StringConstraints(
        pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*(/[a-zA-Z0-9][a-zA-Z0-9_.-]*)*$", max_length=240
    ),
    AfterValidator(_portable_key),
]
type FileOffset = Annotated[int, Field(ge=0)]
type ReadLimit = Annotated[int, Field(ge=1, le=16_000)]
_CONFIG = ConfigDict(strict=True, hide_input_in_errors=True)
_KEY = TypeAdapter(FileKey, config=_CONFIG)
_RANGE = TypeAdapter(tuple[FileOffset, ReadLimit], config=_CONFIG)


def validate_file_key(key: str) -> str:
    return _KEY.validate_python(key)


def validate_file_range(offset: int, limit: int) -> tuple[int, int]:
    return _RANGE.validate_python((offset, limit))


@dataclass(frozen=True, slots=True)
class StoredFile:
    key: str
    size_bytes: int
    modified_ns: int


class SessionFiles(Protocol):
    """A session namespace; keys never refer to files belonging to another session."""

    async def put(self, key: str, text: str) -> None: ...
    async def read(self, key: str, *, offset: int = 0, limit: int = 4000) -> str: ...
    async def remove(self, key: str) -> None: ...
    async def entries(self) -> tuple[StoredFile, ...]: ...


class MemoryFiles:
    """Artifacts retained alongside the in-memory log across live session leases."""

    def __init__(self) -> None:
        self._values: dict[str, tuple[str, int]] = {}

    async def put(self, key: str, text: str) -> None:
        key = validate_file_key(key)
        if not isinstance(text, str):
            raise TypeError("Session file content must be text")
        self._values[key] = text, time_ns()

    async def read(self, key: str, *, offset: int = 0, limit: int = 4000) -> str:
        key = validate_file_key(key)
        offset, limit = validate_file_range(offset, limit)
        try:
            return self._values[key][0][offset : offset + limit]
        except KeyError:
            raise FileNotFoundError(key) from None

    async def remove(self, key: str) -> None:
        self._values.pop(validate_file_key(key), None)

    async def entries(self) -> tuple[StoredFile, ...]:
        return tuple(
            StoredFile(key, len(text.encode("utf-8")), modified)
            for key, (text, modified) in sorted(self._values.items())
        )
