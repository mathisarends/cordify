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


@dataclass(frozen=True)
class TextValue:
    key: FileKey
    text: str
    modified_ns: int

    def __post_init__(self) -> None:
        validate_file_key(self.key)
        if (
            not isinstance(self.text, str)
            or type(self.modified_ns) is not int
            or self.modified_ns < 0
        ):
            raise ValueError("Text resources require string content and a non-negative timestamp")


class SessionFiles(Protocol):
    """A session namespace; keys never refer to files belonging to another session."""

    async def put(self, key: str, text: str) -> None: ...
    async def read(self, key: str, *, offset: int = 0, limit: int = 4000) -> str: ...
    async def remove(self, key: str) -> None: ...
    async def entries(self) -> tuple[StoredFile, ...]: ...


class MemoryFiles:
    """Text resources owned by one live conversation."""

    def __init__(self, values: tuple[TextValue, ...] = ()) -> None:
        self._values: dict[str, tuple[str, int]] = {
            value.key: (value.text, value.modified_ns) for value in values
        }

    def snapshot(self, keys: set[str]) -> tuple[TextValue, ...]:
        return tuple(
            TextValue(key, *self._values[key]) for key in sorted(keys) if key in self._values
        )

    def retain(self, keys: set[str]) -> None:
        self._values = {key: value for key, value in self._values.items() if key in keys}

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

    def clear(self) -> None:
        """Release the live session's text resources."""
        self._values.clear()

    async def entries(self) -> tuple[StoredFile, ...]:
        return tuple(
            StoredFile(key, len(text.encode("utf-8")), modified)
            for key, (text, modified) in sorted(self._values.items())
        )
