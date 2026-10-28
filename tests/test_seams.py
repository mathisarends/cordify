from pathlib import Path
from typing import Protocol

import pytest

from harnessed import Harness, ModelChunk, ToolCall, plugin
from harnessed.core.compaction import Summarizer
from harnessed.core.hooks import ToolPolicy
from harnessed.core.messages import PolicyDecision
from harnessed.core.model import Model
from harnessed.core.runtime.agent_loop import AgentFactory
from harnessed.core.session.events import ToolReturned
from harnessed.core.session.store import SessionPersistence
from harnessed.kernel import Context
from harnessed.plugins.agents.loop import agent_loop
from harnessed.plugins.filesystem import fs_tools
from harnessed.plugins.services import llm
from harnessed.plugins.shell import shell_tools
from harnessed.seams import (
    FS,
    ApprovalAnswer,
    Approver,
    ProcResult,
    Shell,
    SpillStore,
    TokenCounter,
)
from harnessed.testing import ScriptedModel


@pytest.mark.parametrize(
    "contract",
    [
        FS,
        Model,
        Shell,
        Approver,
        TokenCounter,
        Summarizer,
        SpillStore,
        AgentFactory,
        SessionPersistence,
    ],
)
def test_service_contracts_use_structural_protocols(contract):
    assert Protocol in contract.__bases__


class ForeignFiles:
    def __init__(self):
        self.files = {}

    async def read(self, path: Path, *, offset: int = 0, limit: int | None = None) -> str:
        return self.files[str(path)][offset : None if limit is None else offset + limit]

    async def write(self, path: Path, text: str) -> None:
        self.files[str(path)] = text


class ForeignModel:
    def __init__(self, responses):
        self.backend = ScriptedModel(responses)
        self.info = self.backend.info
        self.requests = self.backend.requests

    async def request(self, request):
        return await self.backend.request(request)

    def stream(self, request):
        return self.backend.stream(request)


async def test_foreign_fs_and_model_work_through_di_without_framework_inheritance():
    files = ForeignFiles()
    model = ForeignModel(
        [
            [ModelChunk(tool_call=ToolCall("write_file", {"path": "note.txt", "text": "hello"}))],
            [ModelChunk(tool_call=ToolCall("read_file", {"path": "note.txt"}))],
            [ModelChunk(text="done")],
        ]
    )

    @plugin
    async def foreign_files(ctx: Context):
        ctx.provide(FS, files)
        yield

    assert ForeignFiles.__bases__ == (object,)
    assert ForeignModel.__bases__ == (object,)
    async with Harness([agent_loop(), llm(model), foreign_files(), fs_tools()]).session(
        "foreign"
    ) as agent:
        result = await agent.run("Write and read the note.")
        assert result.text == "done"
        assert files.files == {"note.txt": "hello"}
        results = [event.result for event in result.events if isinstance(event, ToolReturned)]
        assert results[-1].content == "hello" and not results[-1].is_error
        definition = next(tool for tool in model.requests[0].tools if tool.name == "read_file")
        assert set(definition.parameters["properties"]) == {"path", "limit", "offset"}


class ForeignShell:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def run(self, argv, *, cwd, timeout):
        self.calls.append((argv, cwd, timeout))
        return self.result


class ForeignApprover:
    def __init__(self, decision):
        self.decision = decision
        self.calls = []

    async def ask(self, call, reason):
        self.calls.append((call, reason))
        return self.decision


async def test_foreign_shell_and_approver_are_accepted_structurally(tmp_path):
    shell = ForeignShell(ProcResult(7, "failed test", ""))
    approver = ForeignApprover(ApprovalAnswer.ALLOW)
    call = ToolCall("run_command", {"argv": ["python", "-m", "pytest"]})
    model = ForeignModel([[ModelChunk(tool_call=call)], [ModelChunk(text="reviewed")]])

    @plugin
    async def foreign_services(ctx: Context):
        ctx.provide(Shell, shell)
        ctx.provide(Approver, approver)
        yield

    @plugin(needs=[Approver])
    async def policy(ctx: Context):
        @ctx.on(ToolPolicy)
        async def ask(event, next_):
            return PolicyDecision.ASK

        yield

    assert ForeignShell.__bases__ == (object,)
    assert ForeignApprover.__bases__ == (object,)
    harness = Harness(
        [
            agent_loop(),
            llm(model),
            foreign_services(),
            shell_tools(tmp_path),
            policy(),
        ]
    )
    async with harness.session("foreign-shell") as agent:
        result = await agent.run("Run the tests.")
        returned = next(event.result for event in result.events if isinstance(event, ToolReturned))
        assert returned.exit_code == 7 and returned.is_error
        assert shell.calls == [(["python", "-m", "pytest"], tmp_path.resolve(), 30)]
        assert len(approver.calls) == 1 and approver.calls[0][0] == call
