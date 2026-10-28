"""Run with: uv run python -m examples.16_compaction_policies"""

import asyncio
from typing import TypedDict

from harnessed import (
    ContextBudgetExceeded,
    ContextBudgetGuard,
    PeriodicCompaction,
    SlidingWindowCompaction,
    SystemPrompt,
    ThresholdCompaction,
)
from harnessed.core.session.events import CompactionApplied
from harnessed.core.session.session import SessionLog as Session
from harnessed.presets import minimal
from harnessed.testing import ScriptedLLM


class Retention(TypedDict):
    keep_turns: int
    keep_first_turns: int
    summary_tokens: int


async def compare(name, policy):
    model = ScriptedLLM([f"answer {index}" for index in range(6)])
    harness = minimal(
        model,
        plugins=[policy, ContextBudgetGuard(limit=2500, reserve_tokens=256)],
        context_window=2500,
    )
    async with harness.session(name) as agent:
        agent.ctx[SystemPrompt].section("rules", "Keep the goal and recent work accurate.")
        await agent.run("GOAL: keep the public API stable. " + "detail " * 160)
        goal = next(message for message in agent.log.messages if message.role == "user")
        prefix = agent.log.events
        for index in range(1, 6):
            await agent.run(f"task {index}: " + "detail " * 160)
        applied = [event for event in agent.log.events if isinstance(event, CompactionApplied)]
        print(f"{name}: {len(applied)} compactions")
        for event in applied:
            print(
                f"  {event.policy}: {event.before_tokens} -> {event.after_tokens} estimated input "
                f"tokens; {len(event.preserve_turns)} verbatim turn(s)"
            )
        assert goal in model.requests[-1].messages
        assert agent.log.events[: len(prefix)] == prefix
        assert Session("replay", seed=agent.log.events).messages == agent.log.messages
        assert agent.log.fork("fork").messages == agent.log.messages
        print("  Goal kept verbatim; original log, replay and fork verified.")


async def main():
    retention: Retention = {"keep_turns": 1, "keep_first_turns": 1, "summary_tokens": 48}
    await compare("threshold", ThresholdCompaction(at=0.6, **retention))
    await compare("window", SlidingWindowCompaction(**retention))
    await compare("periodic", PeriodicCompaction(every=3, **retention))

    model = ScriptedLLM([])
    async with minimal(model, plugins=[ContextBudgetGuard(limit=100)]).session("guard") as agent:
        try:
            await agent.run("Current task that cannot be summarized away: " + "x" * 1000)
        except ContextBudgetExceeded as error:
            print(f"Guard stopped the request before calling the provider: {error}")
        assert model.requests == []


if __name__ == "__main__":
    asyncio.run(main())
