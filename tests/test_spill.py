"""Spill follows session storage, retains metadata and applies finite quotas."""

import json

import pytest
from pydantic import ValidationError

from harnessed import Agent, Plugin
from harnessed.core.messages import ImagePart, TextPart, ToolResult
from harnessed.core.session.codec import decode_event, encode_event
from harnessed.core.session.events import SpillCreated, SpillExpired
from harnessed.core.session.files import SessionFiles
from harnessed.core.tools.decorator import tool
from harnessed.core.tools.output_limit import ToolOutputLimit
from harnessed.core.tools.registry import ToolError
from harnessed.plugins import JsonlSessions, Rules, Spill, when
from harnessed.plugins.spill import SpillStore
from harnessed.testing import ScriptedModel, call


class Source(Plugin):
    @tool(readonly=True)
    async def source(self, text: str) -> ToolResult:
        return ToolResult(
            (TextPart(text), ImagePart("https://example.test/image.png")), "error", exit_code=3
        )


def configure(model, *, plugins=()):
    return Agent(model=model, plugins=[Source(), *plugins])


@pytest.mark.parametrize("disk", [False, True])
async def test_spill_is_owned_by_its_session_and_resumes_with_exact_content(tmp_path, disk):
    text = "ä😀line\r\n" * 1000
    storage = [JsonlSessions(tmp_path)] if disk else []
    app = configure(
        ScriptedModel([call("source", text=text), "stored"]),
        plugins=[
            *storage,
            Spill(threshold_tokens=32, preview_tokens=8),
            Rules([when(tool="source", result_tokens_over=100).note("Important note.")]),
        ],
    )
    async with app.session("one") as session:
        result = (await session.run("read")).tool_calls[0][1]
        assert result.spill and result.exit_code == 3 and result.status == "error"
        assert "Important note." in result.text and result.rule_matches
        assert sum(isinstance(p, ImagePart) for p in result.content) == 1
        ref = result.spill
        assert ref.path.startswith("spills/") and str(tmp_path) not in result.text
        assert await session.ctx[SpillStore].read(ref.path, offset=1, limit=2) == "😀l"
        events = session.events
        assert (
            tuple(decode_event(json.loads(json.dumps(encode_event(e)))) for e in events) == events
        )
    if disk:
        app = configure(
            ScriptedModel([]), plugins=[JsonlSessions(tmp_path), Spill(threshold_tokens=32)]
        )
    async with app.session("one") as session:
        assert await session.ctx[SpillStore].read(ref.path, limit=16000) == text
    async with app.session("other") as session:
        with pytest.raises(ToolError, match="this session"):
            await session.ctx[SpillStore].read(ref.path)


async def test_retention_expiration_is_durable_and_cannot_be_reactivated_by_old_results():
    model = ScriptedModel(
        [call("source", text="a" * 1000), "one", call("source", text="b" * 1000), "two"]
    )
    app = configure(model, plugins=[Spill(threshold_tokens=8, max_files=1, max_bytes=1200)])
    async with app.session("quota") as session:
        first = (await session.run("one")).tool_calls[0][1].spill
        second = (await session.run("two")).tool_calls[0][1].spill
        assert first and second
        assert [entry.key for entry in await session.ctx[SessionFiles].entries()] == [second.path]
        assert session.last(SpillExpired).ref == first
        assert await session.ctx[SpillStore].read(second.path, limit=2) == "bb"
        with pytest.raises(ToolError, match="expired"):
            await session.ctx[SpillStore].read(first.path)
    async with app.session("quota") as session:
        with pytest.raises(ToolError, match="expired"):
            await session.ctx[SpillStore].read(first.path)
        assert await session.ctx[SpillStore].read(second.path, limit=2) == "bb"


async def test_spill_budget_falls_back_to_the_final_output_limit_without_losing_metadata():
    app = configure(
        ScriptedModel([call("source", text="x" * 4000), "done"]),
        plugins=[Spill(threshold_tokens=1, max_bytes=100), ToolOutputLimit(256)],
    )
    async with app.session() as session:
        result = (await session.run("read")).tool_calls[0][1]
        assert result.spill is None and result.truncation
        assert len(result.text) <= 256 and "Spill budget" in result.text
        assert result.exit_code == 3 and sum(isinstance(p, ImagePart) for p in result.content) == 1
        assert session.last(SpillCreated) is None and not await session.ctx[SessionFiles].entries()


async def test_recovery_cleans_orphans_and_logs_missing_files(tmp_path):
    app = configure(
        ScriptedModel([call("source", text="x" * 1000), "done"]),
        plugins=[JsonlSessions(tmp_path), Spill(threshold_tokens=8)],
    )
    async with app.session("recover") as session:
        ref = (await session.run("read")).tool_calls[0][1].spill
        files = session.ctx[SessionFiles]
        await files.put("spills/orphan.txt", "not recorded")
        await files.remove(ref.path)
    app = configure(ScriptedModel([]), plugins=[JsonlSessions(tmp_path), Spill(threshold_tokens=8)])
    async with app.session("recover") as session:
        assert not await session.ctx[SessionFiles].entries()
        assert session.last(SpillExpired).reason == "missing"
        with pytest.raises(ToolError):
            await session.ctx[SpillStore].read(ref.path)


async def test_read_spill_is_readonly_has_bounded_schema_and_never_respills():
    model = ScriptedModel([call("source", text="abc" * 1000), "done"])
    app = configure(model, plugins=[Spill(threshold_tokens=1)])
    async with app.session() as session:
        ref = (await session.run("source")).tool_calls[0][1].spill
        model.responses.extend([call("read_spill", path=ref.path, offset=1, limit=2), "read"])
        result = (await session.run("range")).tool_calls[0][1]
        assert result.text == "bc" and result.spill is None and result.status == "ok"
        schema = next(t for t in model.requests[0].tools if t.name == "read_spill").parameters
        assert schema["$defs"]["ReadLimit"]["maximum"] == 16000
        assert schema["$defs"]["FileOffset"]["minimum"] == 0


@pytest.mark.parametrize(
    "options",
    [{"threshold_tokens": 0}, {"max_files": True}, {"max_bytes": -1}, {"preview_tokens": -1}],
)
def test_spill_options_are_declarative_and_strict(options):
    with pytest.raises(ValidationError):
        Spill(**options)


async def test_late_canonical_results_do_not_resurrect_a_durably_expired_reference():
    from harnessed.core.session.events import ToolReturned

    app = configure(
        ScriptedModel([call("source", text="x" * 1000), "done"]),
        plugins=[Spill(threshold_tokens=8)],
    )
    async with app.session("late") as session:
        result = (await session.run("source")).tool_calls[0][1]
        await session.log.append(SpillExpired(result.spill))
        await session.log.append(ToolReturned("late-result", result))
        with pytest.raises(ToolError, match="expired"):
            await session.ctx[SpillStore].read(result.spill.path)
    async with app.session("late") as session:
        assert not await session.ctx[SessionFiles].entries()
        with pytest.raises(ToolError, match="expired"):
            await session.ctx[SpillStore].read(result.spill.path)
