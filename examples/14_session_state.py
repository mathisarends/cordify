"""Run with: uv run python -m examples.14_session_state"""

import asyncio

from harnessed import Agent, SessionState
from harnessed.testing import ScriptedModel


async def main() -> None:
    agent = Agent(model=ScriptedModel(["Remembered.", "Continued.", "Alternative."]))
    async with agent.session() as session:
        await session.context.set("project", "Harnessed")
        result = await session.run("Remember the project.")
        state = session.snapshot()
        assert state == result.state
    payload = state.to_json()  # The application chooses where to store these bytes.
    restored = SessionState.from_json(payload)
    continued = await agent.run("Continue with the project.", state=restored)
    alternative = await agent.run("Explore another approach.", state=restored)
    assert restored == state
    assert continued.state.history[-1].message.text == "Continued."
    assert alternative.state.history[-1].message.text == "Alternative."
    print(continued.text, alternative.text, "Independent continuations from one snapshot.")


if __name__ == "__main__":
    asyncio.run(main())
