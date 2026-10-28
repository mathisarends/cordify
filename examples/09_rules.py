"""Run with: uv run python -m examples.09_rules (commands are simulated)."""

import asyncio

from harnessed import (
    Bundle,
    Harness,
    Inject,
    ModelChunk,
    Plugin,
    ProcResult,
    ReviewToolResult,
    Rules,
    ToolCall,
    hook,
    user,
    when,
)
from harnessed import Session as Agent
from harnessed.core.session.events import InboxAdded, ToolReturned
from harnessed.plugins.agents.loop import agent_loop
from harnessed.plugins.services import llm
from harnessed.testing import ScriptedModel


def is_pytest(call: ToolCall) -> bool:
    return "pytest" in call.command


class TestDiscipline(Plugin):
    def __init__(self):
        self.failures = 0

    @hook(ReviewToolResult)
    async def after_tests(self, event, next_, agent: Inject[Agent]):
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
            await agent.context.set(
                "test-discipline", "Tests failed three times. Reconsider your approach."
            )
        if self.failures >= 5:
            await agent.steer(user("Stop editing. Summarize what you tried and what is wrong."))
        return result


commands = Bundle("simulated-commands")


@commands.tool
async def run_command(argv: list[str]) -> ProcResult:
    """Run the simulated test command."""
    return ProcResult(1, "FAILED tests/test_auth.py::test_login", "")


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
    harness = Harness([agent_loop(), llm(model), commands, policy])
    print(f"\n{name}")
    async with harness.session(name) as agent:
        async for event in agent.stream("Run the tests and investigate failures."):
            if isinstance(event, ToolReturned):
                print(f"exit_code={event.result.exit_code}: {event.result.content}")
            elif isinstance(event, InboxAdded) and event.mode != "followup":
                print(f"{event.mode}: {event.message.text}")


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
                when(tool="read_file", result_tokens_over=20_000).note(
                    "File is large. Use a line range next time."
                ),
            ],
            name="test-discipline-v1",
        ),
    )


if __name__ == "__main__":
    asyncio.run(main())
