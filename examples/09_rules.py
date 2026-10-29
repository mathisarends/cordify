"""Run with: uv run python -m examples.09_rules (commands are simulated)."""

import asyncio

from harnessed import Agent, Plugin, PolicyDecision, Session, ToolCall, ToolResult, ToolStatus, tool
from harnessed.events import InboxAddedEvent, InboxMode, ToolCallFinishedEvent
from harnessed.hooks import ReviewToolResult
from harnessed.kernel import Inject, hook
from harnessed.models import ModelChunk, user
from harnessed.plugins import Approvals
from harnessed.plugins.rules import Rules, when
from harnessed.testing import ScriptedModel


def is_pytest(call: ToolCall) -> bool:
    return "pytest" in call.command


class TestDiscipline(Plugin):
    def __init__(self):
        self.failures = 0

    @hook(ReviewToolResult)
    async def after_tests(self, event, next_, session: Inject[Session]):
        result = await next_()
        if not is_pytest(event.call):
            return result
        if result.exit_code == 0:
            self.failures = 0
            return result
        self.failures += 1
        if self.failures == 1:
            return result.with_note("Read the first failure before editing anything.")
        if self.failures == 3:
            await session.context.set(
                "test-discipline", "Tests failed three times. Reconsider your approach."
            )
        if self.failures >= 5:
            await session.steer(user("Stop editing. Summarize what you tried and what is wrong."))
        return result


class SimulatedCommandsTools(Plugin):
    @tool
    async def run_command(self, argv: list[str]) -> ToolResult:
        """Run the simulated test command."""
        return ToolResult("FAILED tests/test_auth.py::test_login", ToolStatus.ERROR, exit_code=1)


commands = SimulatedCommandsTools()


async def demo(name, policy):
    model = ScriptedModel(
        [
            *[
                [
                    ModelChunk(
                        tool_call=ToolCall("run_command", {"argv": ["python", "-m", "pytest"]})
                    )
                ]
                for _ in range(5)
            ],
            "I will summarize the attempts before editing again.",
        ]
    )
    agent = Agent(model=model, plugins=[Approvals(default=PolicyDecision.ALLOW), commands, policy])
    print(f"\n{name}")
    async with agent.session(name) as session:

        @session.on(InboxAddedEvent)
        def inbox_changed(event):
            if event.mode != InboxMode.FOLLOWUP:
                print(f"{event.mode}: {event.message.text}")

        async for event in session.stream("Run the tests and investigate failures."):
            if isinstance(event, ToolCallFinishedEvent):
                print(f"exit_code={event.result.exit_code}: {event.result.content}")
        assert session.context.values
        assert any(
            isinstance(event, InboxAddedEvent) and event.mode == InboxMode.STEER
            for event in session.events
        )


async def main():
    await demo("class-plugin", TestDiscipline())
    await demo(
        "declarative-rules",
        Rules(
            [
                when(tool="run_command", command_contains="git push").steer(
                    "Never push directly. Open a PR instead."
                ),
                when(tool="run_command", exit_code=lambda code: code != 0, repeated=3).context(
                    "Same command failed three times. Try something different."
                ),
                when(tool="run_command", exit_code=lambda code: code != 0, repeated=5).steer(
                    "Stop editing. Summarize what you tried."
                ),
                when(tool="read_file", result_tokens_over=20000).note(
                    "File is large. Use a line range next time."
                ),
            ],
            name="test-discipline-v1",
        ),
    )


if __name__ == "__main__":
    asyncio.run(main())
