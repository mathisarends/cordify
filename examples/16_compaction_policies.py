"""Run with: uv run python -m examples.16_compaction_policies"""

import asyncio

from harnessed import Agent, events
from harnessed.core.budget import ContextBudgetExceeded
from harnessed.core.messages import Role
from harnessed.core.session.session import SessionLog
from harnessed.plugins import AgentLoop, Compaction, at_ratio, every_turns
from harnessed.testing import ScriptedModel


async def compare(name, trigger):
    model = ScriptedModel([f"answer {index}" for index in range(6)])
    summaries = ScriptedModel(["Preserve the public API and finish the open work."] * 6)
    app = Agent(
        model=model,
        instructions="Keep the goal and recent work accurate.",
        plugins=[
            AgentLoop(context_window=2500, max_tokens=256),
            Compaction(
                trigger=trigger,
                summarizer=summaries,
                keep_turns=1,
                keep_first_turns=1,
                summary_tokens=48,
            ),
        ],
    )
    async with app.session(name) as session:
        await session.run("GOAL: keep the public API stable. " + "detail " * 240)
        goal = next(message for message in session.messages if message.role == Role.USER)
        prefix = session.events
        for index in range(1, 6):
            await session.run(f"task {index}: " + "detail " * 240)
        applied = [
            event for event in session.events if isinstance(event, events.compaction.Applied)
        ]
        print(f"{name}: {len(applied)} compactions")
        for event in applied:
            print(
                f"  {event.before_tokens} -> {event.after_tokens} estimated input tokens; "
                f"{len(event.preserve_turns)} verbatim turn(s)"
            )
        assert applied and goal in model.requests[-1].messages
        assert session.events[: len(prefix)] == prefix
        assert SessionLog("replay", seed=session.events).messages == session.messages
        assert session.fork("fork").messages == session.messages
        print("  Goal kept verbatim; original log, replay and fork verified.")


async def main():
    await compare("threshold", at_ratio(0.6))
    await compare("window", every_turns(1))
    await compare("periodic", every_turns(3))

    # Manual compaction is a core operation and uses the session model by default.
    model = ScriptedModel(["first", "second", "third", "Earlier work summarized."])
    async with Agent(model=model).session("manual") as session:
        for index in range(3):
            await session.run(f"task {index}: " + "detail " * 240)
        await session.pause()
        applied = await session.compact()
        assert applied is not None and session.status == "paused"
        print(f"Manual: {applied.before_tokens} -> {applied.after_tokens} tokens, still paused.")
        await session.resume()

    # The current task and tool schemas are never silently removed to fit a budget.
    model = ScriptedModel([])
    async with Agent(model=model, plugins=[AgentLoop(context_window=100), Compaction()]).session(
        "guard"
    ) as session:
        try:
            await session.run("Current task that cannot be summarized away: " + "x" * 1000)
        except ContextBudgetExceeded as error:
            print(f"Guard stopped the request before calling the provider: {error}")
        assert model.requests == []


if __name__ == "__main__":
    asyncio.run(main())
