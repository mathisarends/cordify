import asyncio
import base64
import json
import threading
from dataclasses import replace

import pytest

from harnessed import Agent, Observation, Plugin
from harnessed.core.assets import AssetRef, _MemoryAssets
from harnessed.core.messages import ImageDetail, ImagePart, ImageReference, TextPart, UserMessage
from harnessed.core.model import ModelRequest
from harnessed.core.session.codec import encode_event
from harnessed.core.session.events import (
    ModelRequestedEvent,
    ModelRequestFailedEvent,
    ObservationRecordedEvent,
    ObservationsIncludedEvent,
    UserMessageEvent,
)
from harnessed.core.session.images import _SessionImages
from harnessed.core.session.session import SessionLog
from harnessed.core.session.store import MemoryPersistence
from harnessed.hooks import Next, PrepareRequest
from harnessed.kernel import hook
from harnessed.plugins import Compaction, JsonlSessions, LatestObservationOnly, MemorySessions
from harnessed.plugins.sessions import Durability, _DirectoryAssets
from harnessed.testing import ExtractiveSummarizer, ScriptedModel


def inline(data, detail=ImageDetail.AUTO):
    return ImagePart("data:image/png;base64," + base64.b64encode(data).decode("ascii"), detail)


def test_persisted_log_requires_explicit_asset_ownership():
    with pytest.raises(ValueError, match="explicit asset store"):
        SessionLog("incomplete", persistence=MemoryPersistence())


async def test_public_asset_access_preserves_storage_ownership_and_recorded_requests(tmp_path):
    async with Agent(model=ScriptedModel(["done"]), plugins=[JsonlSessions(tmp_path)]).session(
        "public-assets"
    ) as session:
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


async def test_jsonl_holds_references_and_deduplicates_observation_and_requests(tmp_path):
    image = inline(b"unique screenshot" * 8192, ImageDetail.HIGH)
    model = ScriptedModel(["first", "second"])
    async with Agent(model=model, plugins=[JsonlSessions(tmp_path)]).session("images") as session:
        session.observe(Observation("screen", (TextPart("state"), image), refs=("frame",)))
        await session.run("inspect")
        await session.run("continue")
        snapshot = session.last(ObservationRecordedEvent).observation
        reference = snapshot.content[1]
        assert isinstance(reference, ImageReference) and reference.detail == ImageDetail.HIGH
        assert await session.assets.read(reference.asset) == b"unique screenshot" * 8192
        for event in session.events:
            assert "data:image/" not in json.dumps(encode_event(event))
        requests = [
            event.request for event in session.events if isinstance(event, ModelRequestedEvent)
        ]
        for request, wire in zip(requests, model.requests, strict=True):
            assert await session.log.resolve_request(request) == wire
        assert session.last(ObservationsIncludedEvent).observations[0].refs == ("frame",)
        assert len(list((tmp_path / "images.assets").iterdir())) == 1
        assert (tmp_path / "images.jsonl").stat().st_size < len(image.url)
    assert session.log._images._cache_size == 0


async def test_resume_loads_no_images_and_only_resolves_selected_snapshot(tmp_path, monkeypatch):
    model = ScriptedModel(["first", "second", "resumed"])
    app = Agent(model=model, plugins=[JsonlSessions(tmp_path), LatestObservationOnly()])
    async with app.session("lazy") as session:
        for data in (b"old", b"current"):
            session.observe(Observation("screen", (inline(data),)))
            await session.run("inspect")
    reads = []
    read = _DirectoryAssets._read

    def track(self, ref):
        reads.append(ref)
        return read(self, ref)

    monkeypatch.setattr(_DirectoryAssets, "_read", track)
    async with app.session("lazy") as resumed:
        assert reads == []
        await resumed.run("continue")
        assert len(reads) == 1
        assert await resumed.assets.read(reads[0]) == b"current"


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
            JsonlSessions(tmp_path),
            Compaction(
                trigger=lambda _: False,
                keep_turns=0,
                summary_tokens=8,
                summarizer=ExtractiveSummarizer(),
            ),
        ],
    )
    async with app.session("compact") as session:
        session.observe(Observation("screen", (TextPart("state " * 500), inline(b"old"))))
        await session.run("inspect")
        old = session.last(ObservationRecordedEvent).observation.content[1].asset
        session.log._images.clear()
        # Neither the text-only summarizer nor the following request needs this file.
        (tmp_path / "compact.assets" / old.sha256).unlink()
        assert await session.compact() is not None
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
    async with Agent(model=model, plugins=[JsonlSessions(tmp_path), Attach()]).session(
        "hook"
    ) as session:
        await session.run("inspect")
        stored = session.last(ModelRequestedEvent).request
        assert isinstance(stored.messages[-1].content[0], ImageReference)
        assert model.requests[-1].messages[-1].content == (inline(b"hook"),)
        assert "data:image/" not in (tmp_path / "hook.jsonl").read_text()


async def test_ephemeral_memory_sessions_release_binary_assets_on_close():
    app = Agent(model=ScriptedModel(["done"]), plugins=[MemorySessions(max_sessions=0)])
    async with app.session("ephemeral") as session:
        ref = await session.assets.put(b"pixels", media_type="image/png")
        await session.run(UserMessage((ImageReference(ref),)))
    with pytest.raises(FileNotFoundError):
        await session.assets.read(ref)


async def test_fork_assets_survive_parent_deletion(tmp_path):
    app = Agent(model=ScriptedModel(["first", "fork"]), plugins=[JsonlSessions(tmp_path)])
    async with app.session("parent") as session:
        ref = await session.assets.put(b"pixels", media_type="image/png")
        await session.run(UserMessage((ImageReference(ref),)))
        branch = session.fork("child")
    async with app.session(branch) as child:
        await app.sessions.delete("parent")
        assert await child.assets.read(ref) == b"pixels"
        await child.run("continue")
    assert not (tmp_path / "parent.assets").exists()
    assert (tmp_path / "child.assets" / ref.sha256).is_file()
    await app.sessions.delete("child")
    assert not (tmp_path / "child.assets").exists()


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
async def test_missing_or_corrupt_assets_fail_before_model_call(tmp_path, damage):
    model = ScriptedModel(["first", "must not be called"])
    app = Agent(model=model, plugins=[JsonlSessions(tmp_path)])
    async with app.session("broken") as session:
        ref = await session.assets.put(b"pixels", media_type="image/png")
        await session.run(UserMessage((ImageReference(ref),)))
    location = tmp_path / "broken.assets" / ref.sha256
    if damage == "missing":
        location.unlink()
    else:
        location.write_bytes(b"xxxxxx")
    async with app.session("broken") as resumed:
        with pytest.raises((FileNotFoundError, ValueError)):
            await resumed.run("continue")
        assert resumed.last(ModelRequestFailedEvent) is not None
    assert len(model.requests) == 1


async def test_failed_asset_write_does_not_publish_referencing_event(tmp_path, monkeypatch):
    store = _DirectoryAssets(tmp_path / "assets")
    log = SessionLog("failed", assets=store)

    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr(store, "_put", fail)
    with pytest.raises(OSError, match="disk full"):
        await log.append(UserMessageEvent(UserMessage((inline(b"pixels"),))))
    assert log.events == ()


async def test_asset_io_runs_off_loop_and_cancellation_drains_atomic_write(tmp_path, monkeypatch):
    store = _DirectoryAssets(tmp_path / "assets", durability=Durability.FSYNC)
    entered, release = threading.Event(), threading.Event()
    original = store._put

    def slow(data, media_type):
        entered.set()
        assert release.wait(5)
        return original(data, media_type)

    monkeypatch.setattr(store, "_put", slow)
    task = asyncio.create_task(store.put(b"pixels", media_type="image/png"))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(list(store._root.iterdir())) == 1
    assert not list(store._root.glob("*.tmp"))


@pytest.mark.parametrize("sha", ["../escape", "A" * 64, "x" * 64, "a" * 63])
def test_asset_references_reject_invalid_digest_paths(sha):
    with pytest.raises(ValueError):
        AssetRef(sha, "image/png", 1)


async def test_external_urls_remain_external_and_reference_detail_does_not_duplicate_bytes(
    tmp_path,
):
    store = _DirectoryAssets(tmp_path / "assets")
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
    assert len(list(store._root.iterdir())) == 1
