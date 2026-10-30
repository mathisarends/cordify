import base64
from collections import OrderedDict
from dataclasses import replace
from functools import partial

from harnessed.core.assets import AssetRef, AssetStore
from harnessed.core.messages import (
    ContentPart,
    ImagePart,
    ImageReference,
    Message,
    ToolMessage,
    UserMessage,
)
from harnessed.core.model import ModelRequest
from harnessed.kernel import run_sync

from .events import (
    ContextMessageEvent,
    InboxAddedEvent,
    ModelRequestedEvent,
    ObservationRecordedEvent,
    SessionEvent,
    ToolCallFinishedEvent,
    ToolExecutionFinishedEvent,
    UserMessageEvent,
)


class _SessionImages:
    """Keep binary data out of history and resolve only selected request images.

    The LRU holds at most cache_bytes of ASCII data URLs, not an unbounded count
    of screenshots, with at most 256 entries to bound metadata overhead too.
    Large images bypass it. No cache entry retains a request.
    """

    def __init__(self, store: AssetStore, *, cache_bytes: int = 16 * 1024 * 1024) -> None:
        if type(cache_bytes) is not int or cache_bytes < 0:
            raise ValueError("Image cache size must be a non-negative integer")
        self._store = store
        self._cache_bytes = cache_bytes
        self._cache: OrderedDict[AssetRef, str] = OrderedDict()
        self._cache_size = 0

    def clear(self) -> None:
        self._cache.clear()
        self._cache_size = 0

    def _remember(self, ref: AssetRef, url: str) -> None:
        previous = self._cache.pop(ref, None)
        if previous is not None:
            self._cache_size -= len(previous)
        if len(url) > self._cache_bytes:
            return
        while self._cache and (
            self._cache_size + len(url) > self._cache_bytes or len(self._cache) >= 256
        ):
            _, evicted = self._cache.popitem(last=False)
            self._cache_size -= len(evicted)
        self._cache[ref] = url
        self._cache_size += len(url)

    async def _retain_content(
        self, content: str | tuple[ContentPart, ...]
    ) -> str | tuple[ContentPart, ...]:
        if isinstance(content, str) or not any(
            isinstance(part, ImagePart) and part.url.startswith("data:") for part in content
        ):
            return content
        parts: list[ContentPart] = []
        changed = False
        for part in content:
            if isinstance(part, ImagePart) and part.url.startswith("data:"):
                header, separator, encoded = part.url.partition(",")
                if (
                    not separator
                    or not header.startswith("data:image/")
                    or not header.endswith(";base64")
                ):
                    raise ValueError("Inline images require a base64 image data URL")
                media_type = header[5:-7]
                data = await run_sync(partial(base64.b64decode, encoded, validate=True))
                ref = await self._store.put(data, media_type=media_type)
                self._remember(ref, part.url)
                parts.append(ImageReference(ref, part.detail))
                changed = True
            else:
                parts.append(part)
        return tuple(parts) if changed else content

    async def retain_message[M: Message](self, message: M) -> M:
        if isinstance(message, UserMessage | ToolMessage):
            content = await self._retain_content(message.content)
            if content is not message.content:
                return replace(message, content=content)
        return message

    async def retain_request(self, request: ModelRequest) -> ModelRequest:
        if not any(
            isinstance(part, ImagePart) and part.url.startswith("data:")
            for message in request.messages
            if not isinstance(message.content, str)
            for part in message.content
        ):
            return request
        messages = tuple([await self.retain_message(message) for message in request.messages])
        return (
            request
            if all(a is b for a, b in zip(messages, request.messages, strict=True))
            else replace(request, messages=messages)
        )

    async def retain_event(self, event: SessionEvent) -> SessionEvent:
        if isinstance(event, InboxAddedEvent):
            message = await self.retain_message(event.message)
            context = tuple([await self.retain_message(item) for item in event.context])
            if message is not event.message or any(
                a is not b for a, b in zip(context, event.context, strict=True)
            ):
                return replace(event, message=message, context=context)
        elif isinstance(event, UserMessageEvent | ContextMessageEvent):
            message = await self.retain_message(event.message)
            if message is not event.message:
                return replace(event, message=message)
        elif isinstance(event, ObservationRecordedEvent):
            content = await self._retain_content(event.observation.content)
            if content is not event.observation.content:
                return replace(event, observation=replace(event.observation, content=content))
        elif isinstance(event, ToolExecutionFinishedEvent | ToolCallFinishedEvent):
            content = await self._retain_content(event.result.content)
            if content is not event.result.content:
                return replace(event, result=replace(event.result, content=content))
        elif isinstance(event, ModelRequestedEvent):
            request = await self.retain_request(event.request)
            if request is not event.request:
                return replace(event, request=request)
        return event

    async def resolve(self, request: ModelRequest) -> ModelRequest:
        """Materialize transport images without placing their bytes in the log."""
        references = {
            part.asset
            for message in request.messages
            if not isinstance(message.content, str)
            for part in message.content
            if isinstance(part, ImageReference)
        }
        if not references:
            return request
        # Preserve warm entries needed by this request even if earlier cache misses
        # evict them. The map also shares oversized repeated images within this call.
        resolved = {ref: self._cache[ref] for ref in references if ref in self._cache}
        messages: list[Message] = []
        for message in request.messages:
            if not isinstance(message, UserMessage | ToolMessage) or isinstance(
                message.content, str
            ):
                messages.append(message)
                continue
            parts: list[ContentPart] = []
            changed = False
            for part in message.content:
                if isinstance(part, ImageReference):
                    ref = part.asset
                    url = resolved.get(ref)
                    if url is None:
                        data = await self._store.read(ref)
                        encoded = await run_sync(partial(base64.b64encode, data))
                        url = f"data:{ref.media_type};base64,{encoded.decode('ascii')}"
                        self._remember(ref, url)
                    elif ref in self._cache:
                        self._cache.move_to_end(ref)
                    resolved[ref] = url
                    parts.append(ImagePart(url, part.detail))
                    changed = True
                else:
                    parts.append(part)
            messages.append(replace(message, content=tuple(parts)) if changed else message)
        return replace(request, messages=tuple(messages))
