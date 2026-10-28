import asyncio
import base64
import json
import mimetypes
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from harnessed.core.messages import (
    ContentPart,
    ImagePart,
    Message,
    TextPart,
    User,
    freeze,
    system,
    to_dict,
    user,
)


@dataclass(frozen=True)
class File:
    path: str | Path
    encoding: str = "utf-8"

    async def parts(self) -> tuple[ContentPart, ...]:
        path = Path(self.path)
        text = await asyncio.to_thread(path.read_text, encoding=self.encoding)
        return (TextPart(f"File: {path.name}\n{text}"),)


@dataclass(frozen=True)
class Image:
    path: str | Path
    detail: Literal["auto", "low", "high", "original"] = "auto"

    async def parts(self) -> tuple[ContentPart, ...]:
        location = str(self.path)
        if location.startswith(("https://", "http://", "data:image/")):
            return (ImagePart(location, self.detail),)
        path = Path(self.path)
        mime, _ = mimetypes.guess_type(path.name)
        if mime not in ("image/png", "image/jpeg", "image/gif", "image/webp"):
            raise ValueError("Images must be PNG, JPEG, GIF or WebP")
        data = await asyncio.to_thread(path.read_bytes)
        encoded = base64.b64encode(data).decode("ascii")
        return (ImagePart(f"data:{mime};base64,{encoded}", self.detail),)


type Attachment = File | Image


async def prepare_input(
    content: str | Message,
    *,
    attachments: Iterable[Attachment] = (),
    context: Mapping[str, Any] | None = None,
) -> tuple[Message, tuple[Message, ...]]:
    message = user(content) if isinstance(content, str) else content
    attachments = tuple(attachments)
    if attachments:
        if message.role != "user":
            raise ValueError("Attachments belong to user messages")
        parts = (
            [TextPart(message.content)]
            if isinstance(message.content, str)
            else list(message.content)
        )
        for attachment in attachments:
            parts.extend(await attachment.parts())
        message = User(tuple(parts), id=message.id)
    contributions = []
    for name, value in (context or {}).items():
        if not isinstance(name, str):
            raise TypeError("Context keys must be strings")
        text = (
            value
            if isinstance(value, str)
            else json.dumps(to_dict(freeze(value)), ensure_ascii=False)
        )
        contributions.append(system(f"Task context [{name}]:\n{text}"))
    return message, tuple(contributions)
