from contextlib import contextmanager

import pytest

from harnessed import Agent, ApprovalAnswer, Plugin, ToolCall
from harnessed.core.model import ModelChunk
from harnessed.core.tools.decorator import tool
from harnessed.core.tracing import Tracer
from harnessed.plugins import Approvals, Compaction, every_turns
from harnessed.testing import ScriptedModel


class Recorder(Plugin, slot="tracer"):
    def __init__(self):
        super().__init__()
        self.active = []
        self.finished = []

    async def setup(self, ctx):
        ctx.provide(Tracer, self)

    @contextmanager
    def span(self, name, **attributes):
        self.active.append(name)
        try:
            yield self
        finally:
            self.finished.append((name, dict(attributes)))
            assert self.active.pop() == name

    def set_attribute(self, name, value):
        pass


async def test_tool_span_starts_after_approval_wait_and_all_spans_close():
    trace = Recorder()

    class Work(Plugin):
        @tool
        async def work(self) -> str:
            assert trace.active == ["turn", "step", "tool"]
            return "ready"

    def approve(request):
        assert trace.active == ["turn", "step", "approval_wait"]
        return ApprovalAnswer.ALLOW

    model = ScriptedModel([[ModelChunk(tool_call=ToolCall("work"))], "done"])
    await Agent(model=model, plugins=[Work(), Approvals(approver=approve), trace]).run("Go")
    names = [name for name, _ in trace.finished]
    assert names.index("approval_wait") < names.index("tool")
    assert names.count("model") == names.count("step") == 2
    assert names[-1] == "turn" and trace.active == []


async def test_failed_model_closes_every_span():
    trace = Recorder()
    with pytest.raises(RuntimeError, match="ran out"):
        await Agent(model=ScriptedModel([]), plugins=[trace]).run("Go")
    assert trace.active == []
    assert [name for name, _ in trace.finished] == ["model", "step", "turn"]


async def test_ineffective_compaction_backs_off_and_has_its_own_span(caplog):
    class Ineffective:
        async def summarize(self, messages, *, max_tokens):
            return "long summary " * 10_000

    class Work(Plugin):
        @tool(readonly=True)
        async def work(self) -> str:
            return "small"

    trace = Recorder()
    model = ScriptedModel(
        [
            "short",
            [ModelChunk(tool_call=ToolCall("work"))],
            [ModelChunk(tool_call=ToolCall("work"))],
            "done",
            "third",
            "fourth",
        ]
    )
    app = Agent(
        model=model,
        plugins=[
            trace,
            Work(),
            Compaction(
                trigger=every_turns(1),
                summarizer=Ineffective(),
                keep_turns=0,
                summary_tokens=40_000,
            ),
        ],
    )
    async with app.session() as session:
        await session.run("first")
        await session.run("second")
        assert [name for name, _ in trace.finished].count("compaction") == 1
        await session.run("third")
        assert [name for name, _ in trace.finished].count("compaction") == 1
        await session.run("fourth")
        assert [name for name, _ in trace.finished].count("compaction") == 2
    assert "did not reduce context" in caplog.text
