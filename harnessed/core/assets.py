import hashlib
import re
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class AssetRef:
    """Content identity, independent of storage location or temporary URLs."""

    sha256: str
    media_type: str
    size_bytes: int

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[0-9a-f]{64}", self.sha256):
            raise ValueError("Asset digest must be a lowercase SHA-256")
        if not re.fullmatch(r"[a-z0-9.+-]+/[a-z0-9.+-]+", self.media_type):
            raise ValueError("Asset media type must be a MIME type")
        if type(self.size_bytes) is not int or self.size_bytes < 0:
            raise ValueError("Asset size must be a non-negative integer")

    def verify(self, data: bytes) -> None:
        """Raise ValueError if bytes differ in size or digest; usable by custom stores."""
        if len(data) != self.size_bytes or hashlib.sha256(data).hexdigest() != self.sha256:
            raise ValueError(f"Asset content does not match {self.sha256}")


class AssetStore(Protocol):
    """Immutable binary assets in one session namespace.

    Put must complete before an event references the asset. Reads verify identity;
    missing or corrupt data must fail rather than silently change model input.
    """

    async def put(self, data: bytes, *, media_type: str) -> AssetRef: ...
    async def read(self, ref: AssetRef) -> bytes: ...


class _MemoryAssets:
    def __init__(self) -> None:
        self._values: dict[str, bytes] = {}

    def _clear(self) -> None:
        self._values.clear()

    async def put(self, data: bytes, *, media_type: str) -> AssetRef:
        if not isinstance(data, bytes):
            raise TypeError("Asset content must be bytes")
        ref = AssetRef(hashlib.sha256(data).hexdigest(), media_type, len(data))
        self._values.setdefault(ref.sha256, data)
        return ref

    async def read(self, ref: AssetRef) -> bytes:
        try:
            data = self._values[ref.sha256]
        except KeyError:
            raise FileNotFoundError(ref.sha256) from None
        ref.verify(data)
        return data


__all__ = ["AssetRef", "AssetStore"]
