"""Run with: uv run python -m examples.19_approvals_and_priorities"""

import asyncio
from time import perf_counter

from harnessed import Agent as Application
from harnessed import (
    ApprovalAnswer,
    ApprovalRequest,
    ApprovalUnavailable,
    ModelChunk,
    ToolCall,
    ToolPolicy,
)
from harnessed.core.plugin import Bundle
from harnessed.core.session.events import ToolReturned
from harnessed.plugins import AgentLoop, Approvals
from harnessed.plugins.permissions.approval import approval_gate
from harnessed.testing import ScriptedModel


async def without_ui():
    tools = Bundle("headless-tools")

    @tools.tool
    async def publish() -> str:
        raise AssertionError("Publishing requires approval")

    model = ScriptedModel([[ModelChunk(tool_call=ToolCall("publish"))], "Approval unavailable."])
    async with Application(
        model=model, plugins=[Approvals(trust="all"), tools, approval_gate(risky={"publish"})]
    ).session("no-ui") as agent:
        result = await agent.run("Publish the artifact.")
        returned = next(event for event in result.events if isinstance(event, ToolReturned))
        assert returned.result.status == "unavailable"
        assert not agent.log.approvals.pending
        print(f"No UI: {returned.result.status}; action was not executed.")


async def listener_and_priorities():
    extensions = Bundle("configured")
    tools = Bundle("approved-tools")
    order = []

    @tools.tool
    async def publish() -> str:
        order.append("execute")
        return "Artifact published by the offline example."

    @tools.on(ToolPolicy, priority=0)
    async def timing(event, next_):
        order.append("timer")
        start = perf_counter()
        result = await next_()
        print(f"Downstream policy time: {(perf_counter() - start) * 1000:.1f} ms")
        return result

    @tools.on(ToolPolicy, priority=100)
    async def check(event, next_):
        order.append("policy")
        await asyncio.sleep(0.005)
        return await next_()

    model = ScriptedModel([[ModelChunk(tool_call=ToolCall("publish"))], "Published."])
    harness = Application(
        model=model,
        plugins=[
            Approvals(trust="all"),
            tools,
            approval_gate(risky={"publish"}, priority=-100),
            extensions,
        ],
    )

    @extensions.on(ApprovalRequest)
    async def answer(event):
        order.append("approval")
        start = perf_counter()
        await asyncio.sleep(0.025)
        print(f"Approval response time: {(perf_counter() - start) * 1000:.1f} ms")
        return ApprovalAnswer.ALLOW

    async with harness.session("answered") as agent:
        await agent.run("Publish the artifact.")
        assert order == ["approval", "timer", "policy", "execute"]
        print("Order:", " -> ".join(order))


async def response_timeout():
    stopped = asyncio.Event()
    async with Application(
        model=ScriptedModel([]),
        plugins=[AgentLoop(approval_timeout=0.02), Approvals(trust="all", timeout=0.02)],
    ).session("timeout") as agent:

        @agent.on(ApprovalRequest)
        async def disconnected(event):
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        try:
            await agent.request_approval(ToolCall("publish"), "Publish the artifact?")
        except ApprovalUnavailable as error:
            print(f"SDK: {error}")
        assert stopped.is_set() and (not agent.log.approvals.pending)
        assert agent.log.approvals.records[-1].resolution.status == "unavailable"


async def main():
    await without_ui()
    await listener_and_priorities()
    await response_timeout()


if __name__ == "__main__":
    asyncio.run(main())
