import base64
from dataclasses import replace

import pytest

from harnessed import Agent, Observation, Plugin
from harnessed.core.assets import AssetRef, _MemoryAssets
from harnessed.core.messages import ImageDetail, ImagePart, ImageReference, TextPart, UserMessage
from harnessed.core.model import ModelRequest
from harnessed.core.session.events import (
    ModelRequestedEvent,
    ObservationRecordedEvent,
    ObservationsIncludedEvent,
)
from harnessed.core.session.images import _SessionImages
from harnessed.hooks import Next, PrepareRequest
from harnessed.kernel import hook
from harnessed.plugins import Compaction
from harnessed.testing import ExtractiveSummarizer, ScriptedModel


def inline(data, detail=ImageDetail.AUTO):
    return ImagePart("data:image/png;base64," + base64.b64encode(data).decode("ascii"), detail)


async def test_public_asset_access_preserves_storage_ownership_and_recorded_requests(tmp_path):
    async with Agent(model=ScriptedModel(["done"]), plugins=[]).session() as session:
        backend = session.assets
        assert session.log.assets is backend
        ref = await backend.put(b"pixels", media_type="image/png")
        ref.verify(await backend.read(ref))
        with pytest.raises(ValueError):
            ref.verify(b"wrong pixels")
        with pytest.raises(AttributeError):
            session.assets = _MemoryAssets()
        with pytest.raises(AttributeError):
            session.log.assets = _MemoryAssets()
        assert session.assets is session.log.assets is backend
        await session.run(UserMessage((ImageReference(ref),)))
        recorded = session.last(ModelRequestedEvent).request
        wire = await session.log.resolve_request(recorded)
        assert wire.messages[0].content == (inline(b"pixels"),)
        assert recorded.messages[0].content == (ImageReference(ref),)
        assert session.last(ModelRequestedEvent).request is recorded


class CountingAssets(_MemoryAssets):
    def __init__(self):
        super().__init__()
        self.reads = 0

    async def read(self, ref):
        self.reads += 1
        return await super().read(ref)


async def test_cache_is_byte_bounded_reuses_active_images_and_bypasses_large_images():
    store = CountingAssets()
    images = _SessionImages(store, cache_bytes=100)
    first = await images._retain_content((inline(b"a" * 30),))
    request = ModelRequest((UserMessage(first),))
    await images.resolve(request)
    await images.resolve(request)
    assert store.reads == 0  # Ingestion already populated the encoded cache.
    await images._retain_content((inline(b"b" * 30),))
    assert images._cache_size <= 100 and len(images._cache) == 1
    await images.resolve(request)
    assert store.reads == 1
    large = await images._retain_content((inline(b"c" * 100),))
    assert all(ref != large[0].asset for ref in images._cache)
    await images.resolve(ModelRequest((UserMessage(large),)))
    assert images._cache_size <= 100
    images.clear()
    assert images._cache_size == 0 and not images._cache


async def test_small_cache_does_not_reread_warm_tail_or_repeated_oversized_image():
    store = CountingAssets()
    images = _SessionImages(store, cache_bytes=100)
    first = await images._retain_content((inline(b"a" * 30),))
    second = await images._retain_content((inline(b"b" * 30),))
    await images.resolve(ModelRequest((UserMessage((*first, *second)),)))
    assert store.reads == 1  # Loading the first image must not lose the warm second.
    large = await images._retain_content((inline(b"c" * 100),))
    wire = await images.resolve(ModelRequest((UserMessage((*large, *large)),)))
    assert store.reads == 2
    assert wire.messages[0].content[0].url is wire.messages[0].content[1].url


async def test_tiny_images_also_have_a_bounded_cache_entry_count():
    images = _SessionImages(_MemoryAssets())
    for index in range(300):
        await images._retain_content((inline(index.to_bytes(2)),))
    assert len(images._cache) == 256


async def test_text_requests_preserve_identity_without_storage_access():
    store = CountingAssets()
    images = _SessionImages(store)
    request = ModelRequest((UserMessage("text"),))
    assert await images.retain_request(request) is request
    assert await images.resolve(request) is request
    assert store.reads == 0


async def test_compaction_does_not_load_images_removed_from_the_request(tmp_path):
    model = ScriptedModel(["first", "after compaction"])
    app = Agent(
        model=model,
        plugins=[
            Compaction(
                trigger=lambda _: False,
                keep_turns=0,
                summary_tokens=8,
                summarizer=ExtractiveSummarizer(),
            )
        ],
    )
    async with app.session() as session:
        session.observe(Observation("screen", (TextPart("state " * 500), inline(b"old"))))
        await session.run("inspect")
        old = session.last(ObservationRecordedEvent).observation.content[1].asset
        session.log._images.clear()
        # Neither the text-only summarizer nor the following request needs this file.
        assert await session.compact() is not None
        with pytest.raises(FileNotFoundError):
            await session.assets.read(old)
        await session.run("continue")
        assert not session.last(ObservationsIncludedEvent).observations
        assert all(
            not isinstance(part, ImagePart)
            for message in model.requests[-1].messages
            if not isinstance(message.content, str)
            for part in message.content
        )


async def test_request_hook_inline_images_are_stored_before_recording(tmp_path):
    class Attach(Plugin):
        @hook(PrepareRequest)
        async def prepare(self, event: PrepareRequest, next_: Next[ModelRequest]) -> ModelRequest:
            request = await next_()
            return replace(request, messages=(*request.messages, UserMessage((inline(b"hook"),))))

    model = ScriptedModel(["done"])
    async with Agent(model=model, plugins=[Attach()]).session() as session:
        await session.run("inspect")
        stored = session.last(ModelRequestedEvent).request
        assert isinstance(stored.messages[-1].content[0], ImageReference)
        assert model.requests[-1].messages[-1].content == (inline(b"hook"),)
        assert not session.snapshot().assets  # Hook-owned images are supplied by the hook again.


@pytest.mark.parametrize("sha", ["../escape", "A" * 64, "x" * 64, "a" * 63])
def test_asset_references_reject_invalid_digest_paths(sha):
    with pytest.raises(ValueError):
        AssetRef(sha, "image/png", 1)


async def test_external_urls_remain_external_and_reference_detail_does_not_duplicate_bytes(
    tmp_path,
):
    store = _MemoryAssets()
    images = _SessionImages(store)
    remote = (ImagePart("https://example.test/frame.png"),)
    assert await images._retain_content(remote) is remote
    ref = await store.put(b"pixels", media_type="image/png")
    first = ImageReference(ref, ImageDetail.LOW)
    request = ModelRequest((UserMessage((first, replace(first, detail=ImageDetail.HIGH))),))
    resolved = await images.resolve(request)
    assert [part.detail for part in resolved.messages[0].content] == [
        ImageDetail.LOW,
        ImageDetail.HIGH,
    ]
    assert len(store._values) == 1
