from collections.abc import Awaitable, Callable
from dataclasses import replace

import pytest

from harnessed import Agent, Observation, Plugin, tool
from harnessed.events import (
    CompactionAppliedEvent,
    IncludedObservation,
    ModelRequestedEvent,
    ObservationForm,
    ObservationRecordedEvent,
    ObservationsIncludedEvent,
)
from harnessed.hooks import ConfigureRequest, PrepareRequest, PreStep, StepDecision
from harnessed.kernel import hook
from harnessed.plugins import Compaction, LatestObservationOnly, every_turns
from harnessed.testing import ExtractiveSummarizer, ScriptedModel, call, reply


class Observe(Plugin):
    @hook(PreStep)
    async def observe(
        self, event: PreStep, next_: Callable[[], Awaitable[StepDecision]]
    ) -> StepDecision:
        step = event.step
        event.session.observe(
            Observation(
                "browser.nudges", f"full {step}", f"summary {step}", refs=(f"nudge-{step}",)
            )
        )
        return await next_()

    @tool(readonly=True)
    async def work(self) -> str:
        return "done"


@pytest.mark.parametrize("max_summaries", [0, 1])
async def test_inclusion_tracks_full_and_summary_forms_and_preserves_refs(max_summaries):
    async with Agent(
        model=ScriptedModel([call(Observe.work), reply("done")]),
        plugins=[Observe(), LatestObservationOnly(max_summaries=max_summaries)],
    ).session() as session:
        await session.run("go")
        included = [e for e in session.events if isinstance(e, ObservationsIncludedEvent)]
        assert included[0].observations == (
            IncludedObservation("browser.nudges", ("nudge-1",), ObservationForm.FULL),
        )
        expected = (
            (IncludedObservation("browser.nudges", ("nudge-1",), ObservationForm.SUMMARY),)
            if max_summaries
            else ()
        )
        assert included[1].observations == (
            *expected,
            IncludedObservation("browser.nudges", ("nudge-2",), ObservationForm.FULL),
        )
        for index, event in enumerate(session.events):
            if isinstance(event, ModelRequestedEvent):
                following = session.events[index + 1]
                assert isinstance(following, ObservationsIncludedEvent)
                assert following.request_id == event.request_id
        assert session.last(ObservationRecordedEvent).observation.refs == ("nudge-2",)


@pytest.mark.parametrize("stage", [PrepareRequest, ConfigureRequest])
@pytest.mark.parametrize("edit", [False, True])
async def test_final_request_hooks_remove_provenance_when_observation_is_removed_or_edited(
    stage, edit
):
    class Filter(Plugin):
        @hook(stage)
        async def filter(self, event, next_):
            request = await next_()
            messages = tuple(
                replace(message, content="redacted") if edit else message
                for message in request.messages
                if edit or not message.id.startswith("observation:")
            )
            return replace(request, messages=messages)

    async with Agent(
        model=ScriptedModel(["done"]), plugins=[Observe(), Filter()]
    ).session() as session:
        await session.run("nudge-1 appears here as ordinary user text")
        assert session.last(ObservationsIncludedEvent).observations == ()


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
        session.observe(
            Observation("browser.nudges", "full " * 500, "summary " * 500, refs=("old",))
        )
        await session.run("large " * 500)
        await session.run("continue")
        assert session.last(CompactionAppliedEvent) is not None
        assert not any(
            message.id.startswith("observation:") for message in model.requests[-1].messages
        )
        assert session.last(ObservationsIncludedEvent).observations == ()


async def test_observation_refs_and_inclusion_roundtrip_through_jsonl(tmp_path):
    from harnessed.plugins import JsonlSessions

    app = Agent(model=ScriptedModel(["done"]), plugins=[JsonlSessions(tmp_path)])
    async with app.session("observations") as session:
        session.observe(Observation("browser.nudges", "full", "summary", refs=("a", "b")))
        await session.run("go")
        original = session.events
    async with app.session("observations") as restored:
        assert restored.events == original
        assert restored.last(ObservationsIncludedEvent).observations == (
            IncludedObservation("browser.nudges", ("a", "b"), ObservationForm.FULL),
        )
