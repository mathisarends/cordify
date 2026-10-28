"""Run with: uv run python -m examples.19_approvals_and_priorities"""

import asyncio
from time import perf_counter

from harnessed import (
    ApprovalRequest,
    ApprovalUnavailable,
    Bundle,
    Decision,
    ModelChunk,
    ToolCall,
    ToolPolicy,
)
from harnessed.core.events import ToolReturned
from harnessed.plugins.permissions.approval import approval_gate
from harnessed.presets import headless, minimal
from harnessed.testing import ScriptedLLM


async def without_ui():
    tools = Bundle("headless-tools")

    @tools.tool
    async def publish() -> str:
        raise AssertionError("Publishing requires approval")

    model = ScriptedLLM([[ModelChunk(tool_call=ToolCall("publish"))], "Approval unavailable."])
    async with headless(model, plugins=[tools, approval_gate(risky={"publish"})]).session(
        "no-ui"
    ) as agent:
        result = await agent.run("Publish the artifact.")
        returned = next(event for event in result.events if isinstance(event, ToolReturned))
        assert returned.result.status == "unavailable"
        assert not agent.session.approvals.pending
        print(f"No UI: {returned.result.status}; action was not executed.")


async def listener_and_priorities():
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

    model = ScriptedLLM([[ModelChunk(tool_call=ToolCall("publish"))], "Published."])
    # Register timing first; the explicit priority still places approval outside it.
    harness = minimal(model, plugins=[tools, approval_gate(risky={"publish"}, priority=-100)])

    @harness.on(ApprovalRequest)
    async def answer(event):
        order.append("approval")
        start = perf_counter()
        await asyncio.sleep(0.025)  # Stand-in for an asynchronous UI or SDK response.
        print(f"Approval response time: {(perf_counter() - start) * 1000:.1f} ms")
        return Decision.ALLOW

    async with harness.session("answered") as agent:
        await agent.run("Publish the artifact.")
        assert order == ["approval", "timer", "policy", "execute"]
        print("Order:", " -> ".join(order))


async def response_timeout():
    stopped = asyncio.Event()
    async with minimal(ScriptedLLM([]), approval_timeout=0.02).session("timeout") as agent:

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
        assert stopped.is_set() and not agent.session.approvals.pending
        assert agent.session.approvals.records[-1].resolution.status == "unavailable"


async def main():
    await without_ui()
    await listener_and_priorities()
    await response_timeout()


if __name__ == "__main__":
    asyncio.run(main())
