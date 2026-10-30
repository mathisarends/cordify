from dataclasses import replace

import pytest

from harnessed import Agent, Observation, Plugin, tool
from harnessed.core.session.codec import EventLogCodec
from harnessed.events import (
    CompactionAppliedEvent,
    IncludedObservation,
    ModelRequestedEvent,
    ObservationRecordedEvent,
    ObservationsIncludedEvent,
)
from harnessed.hooks import ConfigureRequest, Next, PrepareRequest, PreStep, StepDecision
from harnessed.kernel import hook
from harnessed.models import ImagePart, ModelRequest, TextPart, UserMessage
from harnessed.plugins import Compaction, JsonlSessions, LatestObservationOnly, every_turns
from harnessed.testing import ExtractiveSummarizer, ScriptedModel, call, reply


class Observe(Plugin):
    @hook(PreStep)
    async def observe(self, event: PreStep, next_: Next[StepDecision]) -> StepDecision:
        step = event.step
        event.session.observe(
            Observation(
                "browser",
                (
                    TextPart(f"page state {step}"),
                    ImagePart(f"https://example.test/frame-{step}.png"),
                ),
                refs=(f"frame-{step}",),
            )
        )
        return await next_()

    @tool(readonly=True)
    async def work(self) -> str:
        return "done"


def observations(request: ModelRequest) -> tuple[UserMessage, ...]:
    return tuple(
        message
        for message in request.messages
        if isinstance(message, UserMessage) and message.id.startswith("observation:")
    )


@pytest.mark.parametrize("latest_only", [False, True])
async def test_snapshots_and_receipts_retain_text_and_images_together(latest_only):
    model = ScriptedModel([call(Observe.work), reply("done")])
    plugins = [Observe(), LatestObservationOnly()] if latest_only else [Observe()]
    async with Agent(model=model, plugins=plugins).session("observations") as session:
        await session.run("go")
        first = observations(model.requests[0])[0]
        current = observations(model.requests[1])[-1]
        assert observations(model.requests[1]) == ((current,) if latest_only else (first, current))
        assert first.content == (
            TextPart("page state 1"),
            ImagePart("https://example.test/frame-1.png"),
        )
        assert current.content == (
            TextPart("page state 2"),
            ImagePart("https://example.test/frame-2.png"),
        )
        first_receipt = IncludedObservation(
            "browser", ("frame-1",), turn_id=session.turns[0].id, step=1, message_id=first.id
        )
        current_receipt = IncludedObservation(
            "browser", ("frame-2",), turn_id=session.turns[0].id, step=2, message_id=current.id
        )
        included = [e for e in session.events if isinstance(e, ObservationsIncludedEvent)]
        assert included[0].observations == (first_receipt,)
        assert included[1].observations == (
            (current_receipt,) if latest_only else (first_receipt, current_receipt)
        )
        for index, event in enumerate(session.events):
            if isinstance(event, ModelRequestedEvent):
                following = session.events[index + 1]
                assert isinstance(following, ObservationsIncludedEvent)
                assert following.request_id == event.request_id
        assert observations(ModelRequest(session.messages)) == observations(model.requests[1])


async def test_automatic_observations_keep_the_entire_previous_request_prefix():
    model = ScriptedModel([call(Observe.work), reply("done")])
    async with Agent(model=model, plugins=[Observe()]).session() as session:
        await session.run("inspect")
    first, second = model.requests
    assert second.messages[: len(first.messages)] == first.messages
    codec = EventLogCodec()
    initial = ModelRequestedEvent(first, model=model.info, request_id="first")
    codec.encode(initial)
    codec.accept(initial)
    record = codec.encode(ModelRequestedEvent(second, model=model.info, request_id="second"))
    assert record["data"]["request"]["messages"]["prefix"] == len(first.messages)


@pytest.mark.parametrize("stage", [PrepareRequest, ConfigureRequest])
@pytest.mark.parametrize("edit", ["remove", "text", "image"])
async def test_final_request_edits_remove_provenance_for_the_whole_snapshot(stage, edit):
    class Filter(Plugin):
        @hook(stage)
        async def filter(self, event, next_):
            request = await next_()
            messages = []
            for message in request.messages:
                if message.id.startswith("observation:"):
                    if edit == "remove":
                        continue
                    content = (
                        (TextPart("redacted"), *message.content[1:])
                        if edit == "text"
                        else (message.content[0],)
                    )
                    message = replace(message, content=content)
                messages.append(message)
            return replace(request, messages=tuple(messages))

    async with Agent(
        model=ScriptedModel(["done"]), plugins=[Observe(), Filter()]
    ).session() as session:
        await session.run("frame-1 appears here as ordinary user text")
        assert session.last(ObservationsIncludedEvent).observations == ()


async def test_image_only_observation_is_projected_and_has_provenance():
    image = ImagePart("data:image/png;base64,aW1hZ2U=")
    model = ScriptedModel(["done"])
    async with Agent(model=model).session() as session:
        session.observe(Observation("screen", (image,), refs=("screen-1",)))
        await session.run("inspect")
        message = observations(model.requests[0])[0]
        assert message.content == (image,) and not message.text
        included = session.last(ObservationsIncludedEvent).observations
        assert len(included) == 1
        assert included[0].source == "screen" and included[0].refs == ("screen-1",)
        assert included[0].message_id == message.id


async def test_compaction_removes_old_images_in_the_same_request_and_keeps_five_steps():
    fire = iter([False] * 6 + [True, False])
    model = ScriptedModel([*(call(Observe.work) for _ in range(7)), reply("done")])
    async with Agent(
        model=model,
        plugins=[
            Observe(),
            Compaction(
                trigger=lambda _state: next(fire),
                summarizer=ExtractiveSummarizer(),
                keep_turns=0,
                keep_steps=5,
                summary_tokens=8,
            ),
        ],
    ).session() as session:
        await session.run("inspect", max_steps=8)
        before = observations(model.requests[5])
        compacted = observations(model.requests[6])
        assert len(before) == len(compacted) == 6
        # Five completed steps plus the current observation, unchanged byte for byte.
        assert compacted[:-1] == before[1:]
        assert before[0] not in model.requests[6].messages
        assert all(part != before[0].content[1] for m in compacted for part in m.content)
        assert observations(model.requests[7])[:-1] == compacted
        applied = session.last(CompactionAppliedEvent)
        assert applied is not None and applied.after_tokens < applied.before_tokens
        included = [e for e in session.events if isinstance(e, ObservationsIncludedEvent)][6]
        assert [item.step for item in included.observations] == list(range(2, 8))
        assert [item.message_id for item in included.observations] == [m.id for m in compacted]


async def test_compacted_observations_are_not_reported_as_included():
    model = ScriptedModel(["first", "second"])
    async with Agent(
        model=model,
        plugins=[
            Compaction(
                trigger=every_turns(1),
                summarizer=ExtractiveSummarizer(),
                keep_turns=0,
                summary_tokens=8,
            ),
        ],
    ).session() as session:
        session.observe(Observation("browser", "state " * 500, refs=("old",)))
        await session.run("large " * 500)
        await session.run("continue")
        assert session.last(CompactionAppliedEvent) is not None
        assert not observations(model.requests[-1])
        assert session.last(ObservationsIncludedEvent).observations == ()


async def test_snapshots_resume_and_fork_with_images_and_refs(tmp_path):
    model = ScriptedModel(["first", "after resume", "branch"])
    app = Agent(model=model, plugins=[JsonlSessions(tmp_path)])
    image = ImagePart("data:image/png;base64,aW1hZ2U=")
    snapshot = Observation("browser", (TextPart("state"), image), refs=("frame",))
    async with app.session("observations") as session:
        session.observe(snapshot)
        await session.run("go")
        original_snapshot = session.last(ObservationRecordedEvent).observation
        original_events = session.events
        original_receipt = session.last(ObservationsIncludedEvent).observations
        original_message = observations(model.requests[0])[0]
    async with app.session("observations") as restored:
        assert restored.events == original_events
        assert restored.last(ObservationRecordedEvent).observation == original_snapshot
        await restored.run("continue")
        assert observations(model.requests[1]) == (original_message,)
        assert restored.last(ObservationsIncludedEvent).observations == original_receipt
        branch = restored.fork("branch")
    async with app.session(branch) as forked:
        await forked.run("branch")
        assert observations(model.requests[2]) == (original_message,)
        assert forked.last(ObservationsIncludedEvent).observations == original_receipt


async def test_only_the_latest_pending_snapshot_per_source_is_recorded():
    model = ScriptedModel(["done"])
    async with Agent(model=model).session() as session:
        session.observe(Observation("browser", "superseded"))
        session.observe(Observation("browser", "current"))
        session.observe(Observation("screen", (ImagePart("https://example.test/screen.png"),)))
        await session.run("inspect")
        records = [e for e in session.events if isinstance(e, ObservationRecordedEvent)]
        assert [e.observation.source for e in records] == ["browser", "screen"]
        assert observations(model.requests[0])[0].text == "current"
