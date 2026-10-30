"""Run with: uv run python -m examples.17_external_state"""

import asyncio

from harnessed import Agent, SessionState
from harnessed.testing import ScriptedModel


async def main() -> None:
    # A database or object store can implement this application-owned repository.
    repository: dict[str, str] = {}
    conversation_id = "support-4711"
    first = await Agent(model=ScriptedModel(["First answer."])).run("First question.")
    repository[conversation_id] = first.state.to_json()

    state = SessionState.from_json(repository[conversation_id])
    model = ScriptedModel(["Second answer."])
    second = await Agent(model=model).run("Follow up.", state=state)
    repository[conversation_id] = second.state.to_json()
    assert [message.text for message in model.requests[0].messages] == [
        "First question.",
        "First answer.",
        "Follow up.",
    ]
    print(second.text, "Storage and conversation identity belong to the application.")


if __name__ == "__main__":
    asyncio.run(main())
